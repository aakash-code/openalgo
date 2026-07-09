#!/usr/bin/env python3
"""
BUY-ONLY breakout backtest for all Dhan F&O stocks.

Loads NSE symbols from Watchlist_Futures_MAY26.csv (or --csv path),
fetches each stock's 5m data, finds BUY breakout signals and simulates
the trade. Prints ranked per-stock results and saves CSV.

Usage:
  OPENALGO_API_KEY=xxx python strategies/backtest_dhan_buy.py [--date YYYY-MM-DD] [--adx 25] [--rr 2.0]

Config (via env or flags):
  --date     : trading date (default = yesterday IST if before 9:15, else today)
  --adx      : ADX threshold (default 25)
  --rr       : risk:reward target ratio (default 2.0)
  --trail    : trailing mode — none / after_1R / full (default after_1R)
  --kind     : signal kind — double / both (default double)
  --csv      : path to futures watchlist CSV (default: Watchlist_Futures_MAY26.csv)
  --capital  : capital per trade in INR (default 50000)
  --top      : max stocks to fetch (default all)
"""

import argparse
import csv
import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "strat", os.path.join(_HERE, "breakout_intraday_strategy.py")
)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
_FUT_RE = re.compile(r"^(.+?)\d{2}(?:" + MONTHS + r")\d{2}FUT$")

DEFAULT_CSV = os.path.join(
    os.path.expanduser("~"), "Downloads", "Watchlist_Futures_MAY26.csv"
)


def _today_ist():
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _prev_trading_day():
    """Yesterday IST (skip weekends)."""
    d = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30) - timedelta(days=1)
    while d.weekday() >= 5:   # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def load_symbols(csv_path: str) -> list[str]:
    """Extract NSE base symbols from a futures watchlist CSV."""
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    syms = sorted({
        m.group(1)
        for r in rows
        for m in [_FUT_RE.match(r.get("symbol", ""))]
        if m
    })
    return syms


def prep(df, day):
    d = S._normalise(df)
    if d is None:
        return None
    if S.USE_ATR_SL:
        d = S.add_atr(d, S.ATR_LEN)
    d = d[(d["_date"] == day) & (d["_hm"] >= S.SESSION_START_MIN) & (d["_hm"] <= S.SESSION_END_MIN)]
    if len(d) < 4:
        return None
    d = S.add_vwap(d)
    d = S.add_adx(d, S.ADX_LEN)
    return d.reset_index(drop=True)


def signal_long(d, i, start_min, adx_thr, kind):
    """Return BUY signal dict or None. No SHORT signals generated."""
    if i < 2:
        return None
    b, p1, p2 = d.iloc[i], d.iloc[i - 1], d.iloc[i - 2]
    if not (start_min <= int(b["_hm"]) <= S.ENTRY_CUTOFF_MIN):
        return None

    o,  h,  c,  lo = float(b["open"]),  float(b["high"]),  float(b["close"]),  float(b["low"])
    o1, h1, c1, l1 = float(p1["open"]), float(p1["high"]), float(p1["close"]), float(p1["low"])
    o2, h2, c2, l2 = float(p2["open"]), float(p2["high"]), float(p2["close"]), float(p2["low"])

    is_green = c > o
    pr, pg   = c1 < o1, c1 > o1
    p2r      = c2 < o2

    buy1 = (kind != "double") and is_green and pr and c > h1
    buy2 = (kind != "single") and is_green and pg and p2r and c > h2

    if not (buy1 or buy2):
        return None

    vwap = float(b["vwap"]) if pd.notna(b.get("vwap")) else None
    adx  = float(b["adx"])  if pd.notna(b.get("adx"))  else 0.0

    if adx < adx_thr:
        return None
    if vwap is not None and c <= vwap:
        return None

    candle_sl = min(lo, l1) if buy1 else min(lo, l1, l2)
    sl_kind   = "single" if buy1 else "double"
    return {
        "dir": "LONG",
        "sl": candle_sl,
        "sig_bar": i,
        "sig_kind": sl_kind,
        "sig_time": str(b["_dt"]),
        "adx": round(adx, 1),
        "vwap": round(vwap, 2) if vwap else None,
        "entry_price": round(c, 2),
    }


def simulate_classic(d, i, sig, target_rr, trail_mode, capital):
    """Simulate the trade, return dict or None."""
    if i + 1 >= len(d):
        return None
    entry  = float(d.iloc[i]["close"])
    sl     = sig["sl"]
    dist   = abs(entry - sl)
    if dist < 0.01:
        return None

    qty = max(1, int(capital / entry))
    min_sl_dist_pct = S.MIN_SL_DIST_PCT if hasattr(S, "MIN_SL_DIST_PCT") else 0.0
    if min_sl_dist_pct > 0 and dist / entry * 100 < min_sl_dist_pct:
        return None

    target    = entry + target_rr * dist
    cur_sl    = sl
    reached1r = False

    for j in range(i + 1, len(d)):
        bar = d.iloc[j]
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])

        if lo <= cur_sl:
            exit_p, reason = cur_sl, "SL"
            exit_time = str(bar["_dt"])
            break
        if hi >= target:
            exit_p, reason = target, "TARGET"
            exit_time = str(bar["_dt"])
            break
        if hi >= entry + dist:
            reached1r = True
        if trail_mode == "full" and cl > entry:
            cur_sl = max(cur_sl, lo)
        elif trail_mode == "after_1R" and reached1r:
            cur_sl = max(cur_sl, lo)
    else:
        exit_p, reason = float(d.iloc[-1]["close"]), "EOD"
        exit_time = str(d.iloc[-1]["_dt"])

    gross   = (exit_p - entry) * qty
    charges = S.compute_charges(entry, exit_p, qty)
    net     = gross - charges

    return {
        "qty": qty,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "sl_dist_pct": round(dist / entry * 100, 2),
        "target": round(target, 2),
        "exit": round(exit_p, 2),
        "exit_time": exit_time[11:16] if len(exit_time) >= 16 else exit_time,
        "reason": reason,
        "gross": round(gross, 2),
        "charges": round(charges, 2),
        "net": round(net, 2),
        "rr_achieved": round((exit_p - entry) / dist, 2) if dist > 0 else 0,
    }


def run(symbols, date_str, start_min, adx_thr, kind, target_rr, trail_mode, capital):
    print(f"\nFetching 5m data for {len(symbols)} symbols  (date={date_str}) ...\n")

    results = []
    no_data = []
    no_signal = []

    for idx, sym in enumerate(symbols, 1):
        print(f"  [{idx:>3}/{len(symbols)}] {sym:<16}", end="", flush=True)
        df = S.fetch_history(sym, S.INTERVAL, 5)   # 5 days to warm ATR
        if df is None:
            print("no data")
            no_data.append(sym)
            continue
        d = prep(df, date_str)
        if d is None:
            print("no bars")
            no_data.append(sym)
            continue

        # Scan for first BUY signal
        found = False
        for i in range(2, len(d)):
            sig = signal_long(d, i, start_min, adx_thr, kind)
            if sig is None:
                continue
            tr = simulate_classic(d, i, sig, target_rr, trail_mode, capital)
            if tr is None:
                continue
            r = {
                "symbol": sym,
                "sig_time":  sig["sig_time"][11:16],
                "sig_kind":  sig["sig_kind"],
                "adx":       sig["adx"],
                "vwap":      sig["vwap"],
                **tr,
            }
            results.append(r)
            color = "\033[32m" if tr["net"] > 0 else "\033[31m"
            reset = "\033[0m"
            print(f"{color}{tr['reason']:<7} entry={tr['entry']:>8.2f}  exit={tr['exit']:>8.2f}  "
                  f"net={tr['net']:>+8.0f}  ({tr['exit_time']}){reset}")
            found = True
            break

        if not found:
            print("no signal")
            no_signal.append(sym)

    return results, no_data, no_signal


def print_summary(results, date_str, adx_thr, target_rr, trail_mode, kind, capital):
    if not results:
        print("\nNo trades found.")
        return

    df = pd.DataFrame(results).sort_values("net", ascending=False)

    total_net    = df["net"].sum()
    total_gross  = df["gross"].sum()
    total_charges= df["charges"].sum()
    wins         = (df["net"] > 0).sum()
    n            = len(df)
    tgts         = (df["reason"] == "TARGET").sum()
    sls          = (df["reason"] == "SL").sum()
    eods         = (df["reason"] == "EOD").sum()

    print("\n" + "=" * 90)
    print(f"  BUY BREAKOUT BACKTEST  |  {date_str}  |  {n} signals  |  "
          f"adx>={adx_thr}  {kind}  {trail_mode}  {target_rr}R  ₹{capital//1000}k/trade")
    print("=" * 90)
    print(f"  {'SYMBOL':<14} {'SIG':>5} {'KIND':>6} {'ADX':>5} {'ENTRY':>8} {'SL%':>5} "
          f"{'EXIT':>8} {'TIME':>5} {'REASON':<7} {'NET':>9}")
    print("  " + "-" * 86)

    for _, r in df.iterrows():
        clr = "\033[32m" if r["net"] > 0 else "\033[31m"
        rst = "\033[0m"
        print(f"{clr}  {r['symbol']:<14} {r['sig_time']:>5} {r['sig_kind']:>6} "
              f"{r['adx']:>5.1f} {r['entry']:>8.2f} {r['sl_dist_pct']:>4.1f}% "
              f"{r['exit']:>8.2f} {r['exit_time']:>5} {r['reason']:<7} "
              f"{r['net']:>+9.0f}{rst}")

    print("  " + "-" * 86)
    print(f"  {'TOTAL':>50}  gross={total_gross:>+8.0f}  charges={total_charges:>6.0f}  "
          f"NET={total_net:>+9.0f}")
    print(f"\n  Signals: {n}  |  Wins: {wins} ({wins/n*100:.0f}%)  |  "
          f"TARGET: {tgts}  SL: {sls}  EOD: {eods}")
    print("=" * 90)

    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date",    default=None)
    ap.add_argument("--adx",    type=float, default=25.0)
    ap.add_argument("--rr",     type=float, default=2.0)
    ap.add_argument("--trail",  default="after_1R", choices=["none", "after_1R", "full"])
    ap.add_argument("--kind",   default="double",   choices=["double", "both"])
    ap.add_argument("--csv",    default=DEFAULT_CSV)
    ap.add_argument("--capital",type=int,   default=50_000)
    ap.add_argument("--start",  default="0915", help="Session start HHMM")
    ap.add_argument("--top",    type=int, default=9999)
    ap.add_argument("--out",    default="", help="Output CSV path (auto-named if empty)")
    args = ap.parse_args()

    # Determine date
    if args.date:
        date_str = args.date
    else:
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        if now_ist.hour < 9 or (now_ist.hour == 9 and now_ist.minute < 15):
            date_str = _prev_trading_day()
        else:
            date_str = _today_ist()

    # Parse start
    hh, mm = int(args.start[:2]), int(args.start[2:])
    start_min = hh * 60 + mm

    # Load symbols
    if not os.path.exists(args.csv):
        print(f"CSV not found: {args.csv}")
        sys.exit(1)
    symbols = load_symbols(args.csv)[:args.top]
    print(f"Loaded {len(symbols)} symbols from {os.path.basename(args.csv)}")
    print(f"Config: date={date_str}  adx>={args.adx}  {args.kind}  trail={args.trail}  "
          f"target={args.rr}R  capital=₹{args.capital//1000}k")

    results, no_data, no_signal = run(
        symbols, date_str, start_min, args.adx, args.kind,
        args.rr, args.trail, args.capital
    )

    df = print_summary(results, date_str, args.adx, args.rr, args.trail, args.kind, args.capital)

    # Save CSV
    if df is not None and len(df):
        out = args.out or os.path.join(
            os.path.dirname(_HERE),
            f"dhan_buy_backtest_{date_str}.csv"
        )
        df.to_csv(out, index=False)
        print(f"\n  Saved: {out}")
        print(f"  No data: {len(no_data)} stocks  |  No signal: {len(no_signal)} stocks")


if __name__ == "__main__":
    main()
