#!/usr/bin/env python3
"""
Parameter sweep for breakout_intraday_strategy — finds net-positive settings.

Fetches each TradeFinder stock's 5m data ONCE, prepares indicators once, then
evaluates every parameter combination in-memory and ranks configs by net P&L.

TWO MODES (--mode):
  classic   : original levers (fixed target, one trade per stock)
  new       : breakeven SL + time-based exit + optional re-entry per stock
  compare   : run both and print side-by-side summary

Classic levers swept:
  session_start  : 0915 / 0945
  adx_threshold  : 20 / 25 / 30
  trail_mode     : none / after_1R / full
  target_rr      : 1.5 / 2.0 / 3.0
  kind           : both / double

New levers swept:
  session_start  : 0915 / 0945
  adx_threshold  : 20 / 25 / 30
  kind           : both / double
  breakeven_pct  : 0.0 (off) / 0.5 / 1.0 / 1.5   ← move SL to entry after X% move
  exit_hm        : 1510 / 1525                     ← time-based square-off
  allow_reentry  : True / False                    ← take next signal after exit

Usage:
  TF_JWT_TOKEN=... OPENALGO_API_KEY=... \\
      python strategies/breakout_sweep.py [--date YYYY-MM-DD] [--top N] [--mode new|classic|compare]
"""

import argparse
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from itertools import product

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("strat", os.path.join(_HERE, "breakout_intraday_strategy.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def _today_ist():
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def prep(df, day):
    d = S._normalise(df)
    if d is None:
        return None
    # Compute ATR on ALL fetched data first so the ewm is warm before filtering to today.
    if S.USE_ATR_SL:
        d = S.add_atr(d, S.ATR_LEN)
    d = d[(d["_date"] == day) & (d["_hm"] >= S.SESSION_START_MIN) & (d["_hm"] <= S.SESSION_END_MIN)]
    if len(d) < 4:
        return None
    d = S.add_vwap(d)
    d = S.add_adx(d, S.ADX_LEN)
    return d.reset_index(drop=True)


# ── signal detection (shared) ──────────────────────────────────────────────────

def _atr_sl_long(d, i, c):
    if S.USE_ATR_SL and "atr" in d.columns:
        v = d.iloc[i]["atr"]
        if pd.notna(v):
            return round(c - float(v) * S.ATR_MULT, 2)
    return None

def _atr_sl_short(d, i, c):
    if S.USE_ATR_SL and "atr" in d.columns:
        v = d.iloc[i]["atr"]
        if pd.notna(v):
            return round(c + float(v) * S.ATR_MULT, 2)
    return None

def signal_at(d, i, start_min, adx_thr, kind):
    if i < 2:
        return None
    b, p1, p2 = d.iloc[i], d.iloc[i - 1], d.iloc[i - 2]
    if not (start_min <= int(b["_hm"]) <= S.ENTRY_CUTOFF_MIN):
        return None
    o, h, c, lo = float(b["open"]), float(b["high"]), float(b["close"]), float(b["low"])
    o1, h1, c1, l1 = float(p1["open"]), float(p1["high"]), float(p1["close"]), float(p1["low"])
    o2, h2, c2, l2 = float(p2["open"]), float(p2["high"]), float(p2["close"]), float(p2["low"])
    is_green, is_red = c > o, c < o
    pg, pr = c1 > o1, c1 < o1
    p2g, p2r = c2 > o2, c2 < o2
    buy1  = is_green and pr  and c > h1
    sell1 = is_red   and pg  and c < l1
    buy2  = is_green and pg  and p2r and c > h2
    sell2 = is_red   and pr  and p2g and c < l2
    if kind == "single":
        buy2 = sell2 = False
    elif kind == "double":
        buy1 = sell1 = False
    vwap = float(b["vwap"]) if pd.notna(b["vwap"]) else None
    adx  = float(b["adx"])  if pd.notna(b["adx"])  else 0.0
    vbuy  = vwap is not None and c > vwap
    vsell = vwap is not None and c < vwap
    if adx < adx_thr:
        return None
    # VWAP chop filter
    if S.USE_VWAP_CHOP and "vwap" in d.columns:
        start = max(0, i - S.VWAP_CHOP_LOOKBACK + 1)
        w = d.iloc[start:i + 1]
        touches = int(((w["high"] >= w["vwap"]) & (w["low"] <= w["vwap"])).sum())
        if touches > S.VWAP_CHOP_MAX_TOUCHES:
            return None
    if (buy1 or buy2)   and vbuy:
        candle_sl = min(lo, l1) if buy1 else min(lo, l1, l2)
        sl = _atr_sl_long(d, i, c) or candle_sl
        return {"dir": "LONG",  "sl": sl, "sig_bar": i}
    if (sell1 or sell2) and vsell:
        candle_sl = max(h, h1)  if sell1 else max(h, h1, h2)
        sl = _atr_sl_short(d, i, c) or candle_sl
        return {"dir": "SHORT", "sl": sl, "sig_bar": i}
    return None


# ── CLASSIC simulate (fixed target + trail, one trade per stock) ───────────────

def simulate_classic(d, i, sig, target_rr, trail_mode):
    if i + 1 >= len(d):
        return None, i + 1
    entry = float(d.iloc[i]["close"])   # enter at signal bar close (matches Pine / TV chart)
    direction, sl = sig["dir"], sig["sl"]
    dist = abs(entry - sl)
    if dist < 0.01 or qty_zero(entry, sl):
        return None, i + 1
    qty = S.compute_qty(entry, sl)
    if qty <= 0:
        return None, i + 1
    target   = entry + target_rr * dist if direction == "LONG" else entry - target_rr * dist
    cur_sl   = sl
    reached_1r = False
    for j in range(i + 1, len(d)):
        bar = d.iloc[j]
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if direction == "LONG":
            if lo <= cur_sl:
                return _mk(entry, cur_sl, qty, direction, "SL"), j
            if hi >= target:
                return _mk(entry, target, qty, direction, "TARGET"), j
            if hi >= entry + dist:
                reached_1r = True
            if trail_mode == "full" and cl > entry:
                cur_sl = max(cur_sl, lo)
            elif trail_mode == "after_1R" and reached_1r:
                cur_sl = max(cur_sl, lo)
        else:
            if hi >= cur_sl:
                return _mk(entry, cur_sl, qty, direction, "SL"), j
            if lo <= target:
                return _mk(entry, target, qty, direction, "TARGET"), j
            if lo <= entry - dist:
                reached_1r = True
            if trail_mode == "full" and cl < entry:
                cur_sl = min(cur_sl, hi)
            elif trail_mode == "after_1R" and reached_1r:
                cur_sl = min(cur_sl, hi)
    return _mk(entry, float(d.iloc[-1]["close"]), qty, direction, "EOD"), len(d) - 1


# ── NEW simulate (breakeven SL + time-based exit) ─────────────────────────────

def simulate_new(d, i, sig, breakeven_pct, exit_hm):
    """
    breakeven_pct : 0.0 = disabled, else activate breakeven when price moves X% from entry
    exit_hm       : HHMM int (e.g. 1510) — force exit at this time
    Returns (trade_result_or_None, exit_bar_index).
    """
    if i + 1 >= len(d):
        return None, i + 1
    entry = float(d.iloc[i]["close"])   # enter at signal bar close (matches Pine / TV chart)
    direction, sl = sig["dir"], sig["sl"]
    dist = abs(entry - sl)
    if dist < 0.01 or qty_zero(entry, sl):
        return None, i + 1
    qty = S.compute_qty(entry, sl)
    if qty <= 0:
        return None, i + 1

    # Convert HHMM to minutes (same format as _hm column)
    exit_min = (exit_hm // 100) * 60 + (exit_hm % 100)

    cur_sl      = sl
    reached_be  = False   # breakeven activated?

    for j in range(i + 1, len(d)):
        bar = d.iloc[j]
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
        hm = int(bar["_hm"])

        # ── Time-based exit ────────────────────────────────────────────────────
        if hm >= exit_min:
            return _mk(entry, cl, qty, direction, "TIME"), j

        if direction == "LONG":
            # SL hit
            if lo <= cur_sl:
                return _mk(entry, cur_sl, qty, direction, "SL"), j
            # Breakeven activation: price moved breakeven_pct% up from entry
            if breakeven_pct > 0 and not reached_be and hi >= entry * (1 + breakeven_pct / 100):
                cur_sl     = max(cur_sl, entry)
                reached_be = True
            # Trail after breakeven to lock in every new candle low
            if reached_be:
                cur_sl = max(cur_sl, lo)
        else:
            if hi >= cur_sl:
                return _mk(entry, cur_sl, qty, direction, "SL"), j
            if breakeven_pct > 0 and not reached_be and lo <= entry * (1 - breakeven_pct / 100):
                cur_sl     = min(cur_sl, entry)
                reached_be = True
            if reached_be:
                cur_sl = min(cur_sl, hi)

    # Hit end of data without time exit
    return _mk(entry, float(d.iloc[-1]["close"]), qty, direction, "EOD"), len(d) - 1


# ── helpers ───────────────────────────────────────────────────────────────────

def qty_zero(entry, sl):
    dist = abs(entry - sl)
    return dist < 0.01 or entry <= 0


def _mk(entry, exit_p, qty, direction, reason):
    gross = (exit_p - entry) * qty * (1 if direction == "LONG" else -1)
    ch    = S.compute_charges(entry, exit_p, qty)
    return {"net": gross - ch, "gross": gross, "charges": ch, "reason": reason}


# ── trade collectors ──────────────────────────────────────────────────────────

def _bar_hhmm(d, i) -> str:
    """Return 'HH:MM' string for bar i, used in sector snapshot lookups."""
    hm = int(d.iloc[i]["_hm"])
    return f"{hm // 60:02d}:{hm % 60:02d}"


def first_trade_classic(d, sym, start_min, adx_thr, kind, target_rr, trail_mode,
                        sector_snaps=None):
    for i in range(2, len(d)):
        sig = signal_at(d, i, start_min, adx_thr, kind)
        if not sig:
            continue
        # Per-trade sector direction gate (only when sector snapshots available)
        if sector_snaps:
            allowed, _ = S.get_sector_at_time(sector_snaps, sym, _bar_hhmm(d, i), sig["dir"])
            if not allowed:
                continue
        tr, _ = simulate_classic(d, i, sig, target_rr, trail_mode)
        if tr is not None:
            tr["dir"]      = sig["dir"]
            tr["sig_time"] = _bar_hhmm(d, i)
        return tr
    return None


def all_trades_new(d, sym, start_min, adx_thr, kind, breakeven_pct, exit_hm, allow_reentry,
                   sector_snaps=None):
    """Collect all trades for one stock. If allow_reentry, scan for new signal after exit."""
    trades    = []
    scan_from = 2

    while scan_from < len(d) - 1:
        found = False
        for i in range(scan_from, len(d) - 1):
            sig = signal_at(d, i, start_min, adx_thr, kind)
            if sig is None:
                continue
            # Per-trade sector direction gate
            if sector_snaps:
                allowed, _ = S.get_sector_at_time(sector_snaps, sym, _bar_hhmm(d, i), sig["dir"])
                if not allowed:
                    scan_from = i + 1
                    break
            tr, exit_bar = simulate_new(d, i, sig, breakeven_pct, exit_hm)
            if tr is not None:
                trades.append(tr)
                if allow_reentry:
                    scan_from = exit_bar + 1
                else:
                    scan_from = len(d)
                found = True
                break
            else:
                scan_from = i + 1
                break
        if not found:
            break

    return trades


# ── CLASSIC sweep ─────────────────────────────────────────────────────────────

def run_classic(cache, ordered, date_str, sector_snaps=None):
    starts  = [555, 585]                    # 0915 / 0945
    adxs    = [20, 25, 30]
    trails  = ["none", "after_1R", "full"]
    targets = [1.5, 2.0, 3.0]
    kinds   = ["both", "double"]
    caps    = [5, 10, 9999]

    results = []
    for start_min, adx_thr, trail, trr, kind in product(starts, adxs, trails, targets, kinds):
        per_symbol = []
        for sym in ordered:
            tr = first_trade_classic(cache[sym], sym, start_min, adx_thr, kind, trr, trail,
                                     sector_snaps=sector_snaps)
            if tr:
                per_symbol.append(tr)
        if not per_symbol:
            continue
        for cap in caps:
            sel = per_symbol[:cap]
            n   = len(sel)
            net = sum(t["net"] for t in sel)
            gross = sum(t["gross"] for t in sel)
            wins  = sum(1 for t in sel if t["net"] > 0)
            tgts  = sum(1 for t in sel if t["reason"] == "TARGET")
            results.append({
                "start": "0915" if start_min == 555 else "0945",
                "adx": adx_thr, "trail": trail, "rr": trr, "kind": kind,
                "cap": "all" if cap == 9999 else cap,
                "n": n, "win": wins, "tgt": tgts, "gross": gross, "net": net,
                "reentry": False,
            })

    results.sort(key=lambda r: r["net"], reverse=True)

    def row(r):
        return (f"{r['start']:>5} adx>={r['adx']:<2} trail={r['trail']:<8} {r['rr']:>3}R "
                f"{r['kind']:<6} cap={str(r['cap']):<3} | n={r['n']:>3} win={r['win']:>3} "
                f"gross={r['gross']:>+8.0f} NET={r['net']:>+8.0f}")

    print("=" * 96)
    print(f"  CLASSIC — TOP 15  ({date_str}, {len(ordered)} syms, {len(results)} combos)")
    print("=" * 96)
    for r in results[:15]:
        print("  " + row(r))

    # Current live config
    live = next((r for r in results if r["start"] == "0915" and r["adx"] == 25
                 and r["trail"] == "after_1R" and r["rr"] == 2.0 and r["kind"] == "double"
                 and r["cap"] == "all"), None)
    print("\n  LIVE CONFIG (adx25 after_1R 2R double):")
    print("  " + (row(live) if live else "n/a"))

    pos = sum(1 for r in results if r["net"] > 0)
    print(f"\n  {pos}/{len(results)} combos net-positive.")
    print("=" * 96)
    return results


# ── NEW sweep ─────────────────────────────────────────────────────────────────

def run_new(cache, ordered, date_str, sector_snaps=None):
    starts    = [555, 585]             # 0915 / 0945
    adxs      = [20, 25, 30]
    kinds     = ["both", "double"]
    bes       = [0.0, 0.5, 1.0, 1.5]  # breakeven_pct
    exits     = [1510, 1525]           # HHMM exit time
    reentries = [True, False]
    caps      = [5, 10, 9999]

    results = []
    for start_min, adx_thr, kind, be_pct, exit_hm, reentry in product(
            starts, adxs, kinds, bes, exits, reentries):
        per_symbol = []
        for sym in ordered:
            trades = all_trades_new(cache[sym], sym, start_min, adx_thr, kind, be_pct, exit_hm,
                                    reentry, sector_snaps=sector_snaps)
            per_symbol.append((sym, trades))

        # flatten sorted by TF rank
        all_first_trades = [(sym, ts[0]) for sym, ts in per_symbol if ts]
        all_all_trades   = [(sym, t)   for sym, ts in per_symbol for t in ts]

        for cap in caps:
            if reentry:
                # cap = max stocks (all trades from each selected stock)
                sel_syms  = [sym for sym, _ in all_first_trades[:cap]] if cap != 9999 else [sym for sym, _ in all_first_trades]
                sel       = [t for sym, t in all_all_trades if sym in sel_syms]
            else:
                sel_first = [t for _, t in all_first_trades]
                sel       = sel_first[:cap] if cap != 9999 else sel_first

            if not sel:
                continue
            net    = sum(t["net"]   for t in sel)
            gross  = sum(t["gross"] for t in sel)
            wins   = sum(1 for t in sel if t["net"] > 0)
            times  = sum(1 for t in sel if t["reason"] == "TIME")
            n      = len(sel)
            results.append({
                "start": "0915" if start_min == 555 else "0945",
                "adx": adx_thr, "kind": kind,
                "be": be_pct, "exit": exit_hm, "reentry": reentry,
                "cap": "all" if cap == 9999 else cap,
                "n": n, "win": wins, "time_exit": times,
                "gross": gross, "net": net,
            })

    results.sort(key=lambda r: r["net"], reverse=True)

    def row(r):
        be_str  = f"be={r['be']:.1f}%"
        re_str  = "reentry" if r["reentry"] else "once   "
        return (f"{r['start']:>5} adx>={r['adx']:<2} {r['kind']:<6} {be_str:<7} "
                f"exit={r['exit']} {re_str} cap={str(r['cap']):<3} | "
                f"n={r['n']:>3} win={r['win']:>3} time={r['time_exit']:>3} "
                f"gross={r['gross']:>+8.0f} NET={r['net']:>+8.0f}")

    print("=" * 104)
    print(f"  NEW MODE — TOP 20  ({date_str}, {len(ordered)} syms, {len(results)} combos)")
    print(f"  (breakeven SL + time-exit + optional re-entry)")
    print("=" * 104)
    for r in results[:20]:
        print("  " + row(r))

    print("\n  BEST PER CAP:")
    for cap in ["all", 10, 5]:
        best = next((r for r in results if r["cap"] == cap), None)
        if best:
            print(f"  cap={cap:<3} -> " + row(best))

    pos = sum(1 for r in results if r["net"] > 0)
    print(f"\n  {pos}/{len(results)} combos net-positive.")
    print("=" * 104)

    # Highlight: current live settings with new mode (adx25, double, be=1%, exit=1510, reentry)
    equiv = next((r for r in results if r["start"] == "0915" and r["adx"] == 25
                  and r["kind"] == "double" and r["be"] == 1.0
                  and r["exit"] == 1510 and r["reentry"] and r["cap"] == "all"), None)
    print("\n  NEW PROPOSED CONFIG (adx25 double be=1% exit=1510 reentry cap=all):")
    print("  " + (row(equiv) if equiv else "n/a"))
    print("=" * 104)

    return results


# ── main ──────────────────────────────────────────────────────────────────────

def _rank_ordered(ordered, snapshots, rank_cap):
    """
    Re-order `ordered` by their TF rank at 09:15 snapshot (or first available).
    Returns list of (symbol, rank_at_open) filtered to rank <= rank_cap if cap > 0.
    Falls back to original order if no snapshots available.
    """
    if not snapshots:
        return [(s, None) for s in ordered]
    first_snap = snapshots[0]
    ranks = first_snap.get("ranks", {})
    with_rank = sorted([(s, ranks.get(s)) for s in ordered], key=lambda x: x[1] or 9999)
    if rank_cap > 0:
        with_rank = [(s, r) for s, r in with_rank if r is not None and r <= rank_cap]
    return with_rank


def _sector_ordered(ordered, sector_snapshots, top_n):
    """
    Filter `ordered` to stocks whose sector is in top-N long OR top-N short sectors
    at the MORNING (first) snapshot. Returns filtered list preserving original order.
    Falls back to full `ordered` if no snapshots.
    """
    if not sector_snapshots:
        return ordered
    first = sector_snapshots[0]
    long_secs  = set(first.get("long_sectors",  []))
    short_secs = set(first.get("short_sectors", []))
    eligible   = long_secs | short_secs
    stock_map  = first.get("stock_sectors", {})
    return [s for s in ordered if any(m in eligible for m in stock_map.get(s, []))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date",          default=_today_ist())
    ap.add_argument("--top",           type=int, default=60)
    ap.add_argument("--mode",          choices=["classic", "new", "compare"], default="compare")
    ap.add_argument("--symbols",       default="", help="Comma-separated symbols — skips TradeFinder fetch")
    ap.add_argument("--rank-cap",      type=int, default=0,
                    help="Only trade stocks ranked <= N in TF list AT SIGNAL TIME. "
                         "Requires tf_snapshots_DATE.jsonl from the rank poller. 0 = disabled.")
    ap.add_argument("--sector-filter", action="store_true",
                    help="Filter stocks to top/bottom sectors using sector_snapshots_DATE.jsonl. "
                         "Requires the sector poller to have run during market hours.")
    ap.add_argument("--sector-top-n",  type=int, default=2,
                    help="Top N gaining (LONG) + top N losing (SHORT) sectors. Default 2.")
    ap.add_argument("--log-dir",       default="logs/breakout",
                    help="Directory containing snapshot JSONL files")
    args = ap.parse_args()

    # Load TF rank snapshots (for --rank-cap honest replay)
    snapshots = []
    if args.rank_cap > 0:
        snapshots = S.load_tf_snapshots(args.date, args.log_dir)
        if snapshots:
            print(f"Loaded {len(snapshots)} TF snapshots for {args.date} "
                  f"(first={snapshots[0]['time']}, last={snapshots[-1]['time']})")
        else:
            print(f"WARNING: --rank-cap={args.rank_cap} but no tf_snapshots_{args.date}.jsonl found.")
            print(f"  The rank poller must run during market hours to build this file.")
            print(f"  Falling back to EOD TF order (look-ahead biased).")

    # Load sector snapshots (for --sector-filter honest replay)
    sector_snapshots = []
    if args.sector_filter:
        sector_snapshots = S.load_sector_snapshots(args.date, args.log_dir)
        if sector_snapshots:
            print(f"Loaded {len(sector_snapshots)} sector snapshots for {args.date} "
                  f"(first={sector_snapshots[0]['time']}, last={sector_snapshots[-1]['time']})")
            first = sector_snapshots[0]
            print(f"  Morning LONG  sectors: {first.get('long_sectors', [])}")
            print(f"  Morning SHORT sectors: {first.get('short_sectors', [])}")
        else:
            print(f"WARNING: --sector-filter set but no sector_snapshots_{args.date}.jsonl found.")
            print(f"  The sector poller must run during market hours to build this file.")
            print(f"  Falling back to full symbol list (no sector filtering).")

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        stocks = S.fetch_intraday_boost()
        if not stocks:
            print("No TradeFinder stocks (JWT expired?). Use --symbols to bypass.")
            return
        symbols = [s["symbol"] for s in stocks[:args.top]]
    print(f"Fetching + prepping {len(symbols)} symbols for {args.date}...")

    cache = {}
    for sym in symbols:
        df = S.fetch_history(sym, S.INTERVAL, 3)
        if df is None:
            continue
        d = prep(df, args.date)
        if d is not None:
            cache[sym] = d

    ordered = [s for s in symbols if s in cache]

    # Apply TF rank filter (re-order by morning rank, cap at N)
    if snapshots and args.rank_cap > 0:
        ranked  = _rank_ordered(ordered, snapshots, args.rank_cap)
        ordered = [s for s, _ in ranked]
        print(f"Rank-filtered to {len(ordered)} stocks (rank<={args.rank_cap} at market open)")
        if ordered:
            print(f"  Morning top-10: {ordered[:10]}")

    # Apply sector filter (restrict to stocks in active long/short sectors at open)
    if sector_snapshots:
        pre_count = len(ordered)
        ordered   = _sector_ordered(ordered, sector_snapshots, args.sector_top_n)
        print(f"Sector-filtered: {pre_count} → {len(ordered)} stocks "
              f"(top-{args.sector_top_n} long + top-{args.sector_top_n} short sectors)")

    print(f"Usable: {len(ordered)} symbols\n")

    if args.mode in ("classic", "compare"):
        run_classic(cache, ordered, args.date, sector_snapshots)
        print()

    if args.mode in ("new", "compare"):
        run_new(cache, ordered, args.date, sector_snapshots)


if __name__ == "__main__":
    main()
