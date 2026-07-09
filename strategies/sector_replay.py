#!/usr/bin/env python3
"""
Day-by-day sector-strategy replay engine.

Given a captured day, reconstructs exactly what the live strategy would have traded —
no look-ahead. For each 5-min breakout signal it reads the sector snapshot AT THAT TIME
(get_sector_at_time: active sector + breadth + top-N + stock-alignment + r_factor) and,
if allowed, simulates the chosen exit mode. Writes a per-day trade CSV and appends the
day to daily_summary.csv (same schema the live strategy produces), so sector_report.py
can aggregate live + replayed days together.

OHLCV comes from the broker (Upstox via OpenAlgo) at replay time — works within the
broker's ~30-day retention window.

Usage:
    OPENALGO_API_KEY=... uv run python strategies/sector_replay.py --date 2026-06-09
    OPENALGO_API_KEY=... uv run python strategies/sector_replay.py --range 2026-06-09:2026-06-27
    ... [--exit-mode 2R|steplock|eod] [--rr 2.0] [--step-r 1.0] [--lock-start 2.0]
        [--exit-hm 1510] [--capital 50000] [--leverage 5]
"""

import argparse
import csv as _csv
import importlib.util
import os
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B  = _load("strat", "breakout_intraday_strategy.py")
SW = _load("sweep", "breakout_sweep.py")
B.USE_VWAP = os.getenv("USE_VWAP", "false").lower() == "true"   # default off for replay
SW.S.USE_VWAP = B.USE_VWAP


def _bar_hhmm(d, i) -> str:
    hm = int(d.iloc[i]["_hm"])
    return f"{hm // 60:02d}:{hm % 60:02d}"


def _be_price(entry, qty, direction):
    if not B.BREAKEVEN_COVER_CHARGES or qty <= 0:
        return entry
    ps = B.compute_charges(entry, entry, qty) / qty
    return round(entry + ps, 2) if direction == "LONG" else round(entry - ps, 2)


def _simulate(d, i, sig, args):
    """Simulate one trade from signal bar i under the chosen exit mode. Returns dict or None."""
    entry = float(d.iloc[i]["close"]); sl0 = sig["sl"]; dist = abs(entry - sl0)
    if dist < 0.01:
        return None
    qty = max(1, int(args.capital / entry)); dr = sig["dir"]
    exit_min = (args.exit_hm // 100) * 60 + (args.exit_hm % 100)
    tgt = (entry + args.rr * dist) if dr == "LONG" else (entry - args.rr * dist)
    cur_sl = sl0; max_r = 0.0
    exit_p = exit_t = reason = None

    for j in range(i + 1, len(d)):
        bar = d.iloc[j]; hi, lo = float(bar["high"]), float(bar["low"]); hm = int(bar["_hm"])
        t = _bar_hhmm(d, j)
        # Hard time exit
        if hm >= exit_min:
            exit_p, exit_t, reason = float(bar["close"]), t, "EOD"
            break
        # SL hit (current SL, set from prior bars)
        if dr == "LONG" and lo <= cur_sl:
            exit_p, exit_t, reason = cur_sl, t, "SL"; break
        if dr == "SHORT" and hi >= cur_sl:
            exit_p, exit_t, reason = cur_sl, t, "SL"; break
        # Fixed target (2R mode only)
        if args.exit_mode == "2R" and args.rr > 0:
            if dr == "LONG" and hi >= tgt:
                exit_p, exit_t, reason = tgt, t, "TARGET"; break
            if dr == "SHORT" and lo <= tgt:
                exit_p, exit_t, reason = tgt, t, "TARGET"; break
        # Update excursion + step-lock SL for next bar (steplock mode)
        cur_r = (hi - entry) / dist if dr == "LONG" else (entry - lo) / dist
        if cur_r > max_r:
            max_r = cur_r
        if args.exit_mode == "steplock" and max_r >= args.lock_start:
            steps = int((max_r - args.lock_start) / args.step_r)
            lock_r = steps * args.step_r
            if lock_r == 0.0:
                nsl = _be_price(entry, qty, dr)
            elif dr == "LONG":
                nsl = round(entry + lock_r * dist, 2)
            else:
                nsl = round(entry - lock_r * dist, 2)
            cur_sl = max(cur_sl, nsl) if dr == "LONG" else min(cur_sl, nsl)
    else:
        exit_p, exit_t, reason = float(d.iloc[-1]["close"]), _bar_hhmm(d, len(d) - 1), "EOD"

    gross = (exit_p - entry) * qty if dr == "LONG" else (entry - exit_p) * qty
    charges = B.compute_charges(entry, exit_p, qty)
    return {
        "entry_time": _bar_hhmm(d, min(i + 1, len(d) - 1)), "exit_time": exit_t,
        "symbol": sig["sym"], "direction": dr, "entry_price": round(entry, 2),
        "initial_sl": round(sl0, 2), "exit_price": round(exit_p, 2), "qty": qty,
        "notional": round(qty * entry, 0), "gross": round(gross, 0),
        "charges": round(charges, 0), "net": round(gross - charges, 0), "reason": reason,
        "signal_time": _bar_hhmm(d, i),
    }


def replay_day(date_str: str, args) -> dict | None:
    snaps = B.load_sector_snapshots(date_str, args.log_dir)
    if not snaps:
        print(f"  {date_str}: no sector_snapshots — skipped (was the capture daemon running?)")
        return None

    # Universe = every stock that appears in any active sector across the day's snapshots
    universe = set()
    for snap in snaps:
        for s in snap.get("long_sectors", []) + snap.get("short_sectors", []):
            for sym in snap.get("sector_top_n", {}).get(s, []):
                universe.add(sym)
    universe = sorted(universe)
    if not universe:
        print(f"  {date_str}: no active-sector stocks in snapshots — skipped")
        return None

    # Fetch + prep OHLCV for each (broker, ~30-day window)
    cache = {}
    for sym in universe:
        df = B.fetch_history(sym, B.INTERVAL, 5)
        if df is None:
            continue
        d = SW.prep(df, date_str)
        if d is not None:
            cache[sym] = d
    if not cache:
        print(f"  {date_str}: no OHLCV (weekend/holiday or beyond broker retention) — skipped")
        return None

    # Per-day trade CSV (same schema fields append_daily_summary reads)
    trade_csv = os.path.join(args.log_dir, f"replay_trades_{date_str}.csv")
    fields = ["date", "signal_time", "entry_time", "exit_time", "symbol", "direction",
              "entry_price", "initial_sl", "exit_price", "qty", "notional",
              "gross", "charges", "net", "reason"]
    trades = []
    for sym in cache:
        d = cache[sym]
        for i in range(2, len(d)):
            sig = SW.signal_at(d, i, B.SESSION_START_MIN, 0, "both")  # ADX off, both dirs
            if not sig:
                continue
            sig["sym"] = sym
            allowed, _ = B.get_sector_at_time(snaps, sym, _bar_hhmm(d, i), sig["dir"])
            if not allowed:
                continue
            tr = _simulate(d, i, sig, args)
            if tr:
                trades.append(tr)
            break   # first signal per stock per day

    with open(trade_csv, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for tr in trades:
            row = {k: tr.get(k, "") for k in fields}
            row["date"] = date_str
            w.writerow(row)

    summary = B.append_daily_summary(
        trade_csv=trade_csv,
        summary_csv=os.path.join(args.log_dir, "daily_summary.csv"),
        run_date=date_str, dry_run=True,
        config_summary=(f"REPLAY exit={args.exit_mode} rr={args.rr} step={args.step_r} "
                        f"lock_start={args.lock_start} breadth={B.SECTOR_MIN_BREADTH:.0f} "
                        f"top_n={B.SECTOR_TOP_STOCKS_N} min_rf={B.SECTOR_MIN_RFACTOR}"),
        leverage=args.leverage,
    )
    if summary:
        print(f"  {date_str}: {summary['n_trades']} trades  "
              f"win={summary['win_rate']}%  NET=Rs.{summary['net']:,.0f}  "
              f"peak_margin=Rs.{summary['peak_margin']:,.0f}  maxDD=Rs.{summary['max_drawdown_rs']:,.0f}")
    else:
        print(f"  {date_str}: no qualifying trades")
    return summary


def _daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    while d0 <= d1:
        if d0.weekday() < 5:   # skip weekends
            yield d0.strftime("%Y-%m-%d")
        d0 += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--range", dest="rng", help="START:END inclusive (skips weekends)")
    ap.add_argument("--log-dir", default="logs/breakout")
    ap.add_argument("--exit-mode", choices=["2R", "steplock", "eod"], default="2R")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--step-r", type=float, default=1.0)
    ap.add_argument("--lock-start", type=float, default=2.0)
    ap.add_argument("--exit-hm", type=int, default=1510)
    ap.add_argument("--capital", type=float, default=50000)
    ap.add_argument("--leverage", type=float, default=5)
    args = ap.parse_args()
    if args.exit_mode == "eod":
        args.rr = 0   # no target; ride to time exit

    print(f"Replay  exit_mode={args.exit_mode} rr={args.rr} breadth>={B.SECTOR_MIN_BREADTH:.0f}% "
          f"top_n={B.SECTOR_TOP_STOCKS_N}  cap=Rs.{args.capital:.0f} lev={args.leverage:.0f}x")
    print("=" * 80)
    if args.rng:
        a, b = args.rng.split(":")
        for ds in _daterange(a, b):
            replay_day(ds, args)
    elif args.date:
        replay_day(args.date, args)
    else:
        print("Provide --date YYYY-MM-DD or --range START:END")


if __name__ == "__main__":
    main()
