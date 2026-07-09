#!/usr/bin/env python3
"""
VERIFICATION REPORT / single-day replay for breakout_intraday_strategy.

For every trade the live strategy WOULD take on --date, prints the full
Pine-style breakdown so you can cross-check against your TradingView chart:

  signal candle (time + OHLC)  ->  direction/kind
  entry  (next-bar open, with time)
  SL     (which candle low/high + distance in points)
  QTY    (risk-budget qty  vs  notional-cap qty  -> min, exactly like Pine)
  TARGET (entry +/- TARGET_RR x SL-distance)
  exit   (price + time + reason)  ->  gross / charges / net / R-multiple

Also writes breakout_verify_<date>.csv.

Usage:
  TF_JWT_TOKEN=... OPENALGO_API_KEY=... \
      python strategies/breakout_replay.py [--date YYYY-MM-DD] [--top N] [--symbols A,B,C]
"""

import argparse
import csv
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

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
    d = d[(d["_date"] == day) & (d["_hm"] >= S.SESSION_START_MIN) & (d["_hm"] <= S.SESSION_END_MIN)]
    if len(d) < 4:
        return None
    d = S.add_vwap(d)
    d = S.add_adx(d, S.ADX_LEN)
    return d.reset_index(drop=True)


def _t(v):
    s = str(v)
    return s[11:16] if len(s) >= 16 else s


def signal_at(d, i):
    """Mirror live evaluate_signal exactly (uses strategy module's config flags)."""
    if i < 2:
        return None
    b, p1, p2 = d.iloc[i], d.iloc[i - 1], d.iloc[i - 2]
    if not (S.SESSION_START_MIN <= int(b["_hm"]) <= S.ENTRY_CUTOFF_MIN):
        return None
    o, h, c, lo = float(b["open"]), float(b["high"]), float(b["close"]), float(b["low"])
    o1, h1, c1, l1 = float(p1["open"]), float(p1["high"]), float(p1["close"]), float(p1["low"])
    o2, h2, c2, l2 = float(p2["open"]), float(p2["high"]), float(p2["close"]), float(p2["low"])
    is_green, is_red = c > o, c < o
    pg, pr = c1 > o1, c1 < o1
    p2g, p2r = c2 > o2, c2 < o2
    buy1 = S.ENABLE_SINGLE and S.ENABLE_BUY and is_green and pr and c > h1
    sell1 = S.ENABLE_SINGLE and S.ENABLE_SELL and is_red and pg and c < l1
    buy2 = S.ENABLE_DOUBLE and S.ENABLE_BUY and is_green and pg and p2r and c > h2
    sell2 = S.ENABLE_DOUBLE and S.ENABLE_SELL and is_red and pr and p2g and c < l2
    vwap = float(b["vwap"]) if pd.notna(b["vwap"]) else None
    adx = float(b["adx"]) if pd.notna(b["adx"]) else 0.0
    vbuy = (not S.USE_VWAP) or (vwap is not None and c > vwap)
    vsell = (not S.USE_VWAP) or (vwap is not None and c < vwap)
    if S.USE_ADX and adx < S.ADX_THRESHOLD:
        return None
    base = {"adx": adx, "vwap": vwap, "sig_time": str(b["_dt"]),
            "sig_o": o, "sig_h": h, "sig_l": lo, "sig_c": c}
    if (buy1 or buy2) and vbuy:
        sl_src = "min(L,L1)" if buy1 else "min(L,L1,L2)"
        return {**base, "dir": "LONG", "kind": "single" if buy1 else "double",
                "sl": (min(lo, l1) if buy1 else min(lo, l1, l2)), "sl_src": sl_src}
    if (sell1 or sell2) and vsell:
        sl_src = "max(H,H1)" if sell1 else "max(H,H1,H2)"
        return {**base, "dir": "SHORT", "kind": "single" if sell1 else "double",
                "sl": (max(h, h1) if sell1 else max(h, h1, h2)), "sl_src": sl_src}
    return None


def simulate(d, i, sig):
    """Enter next-bar open; walk to SL / target / trail(TRAIL_MODE) / EOD. Full detail."""
    if i + 1 >= len(d):
        return None
    entry_bar = d.iloc[i + 1]
    entry = float(entry_bar["open"])
    direction, sl = sig["dir"], sig["sl"]
    dist = abs(entry - sl)
    if dist < 0.01 or (direction == "LONG" and entry <= sl) or (direction == "SHORT" and entry >= sl):
        return None

    # Pine risk-calculator sizing, components exposed
    risk_budget = S.BALANCE * (S.MAX_LOSS_PCT / 100.0)
    import math
    qty_risk = math.floor(risk_budget / dist)
    qty_cap = math.floor(S.CAPITAL_PER_TRADE / entry)
    qty = max(0, min(qty_risk, qty_cap))
    if qty <= 0:
        return None
    target = entry + S.TARGET_RR * dist if direction == "LONG" else entry - S.TARGET_RR * dist

    cur_sl, reached_1r = sl, False
    for j in range(i + 1, len(d)):
        bar = d.iloc[j]
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if direction == "LONG":
            if lo <= cur_sl:
                return _close(d, i, j, sig, entry, cur_sl, qty, qty_risk, qty_cap, dist, target, "SL")
            if hi >= target:
                return _close(d, i, j, sig, entry, target, qty, qty_risk, qty_cap, dist, target, "TARGET")
            if hi >= entry + dist:
                reached_1r = True
            allow = (S.TRAIL_MODE == "full" and cl > entry) or (S.TRAIL_MODE == "after_1R" and reached_1r)
            if allow:
                cur_sl = max(cur_sl, lo)
        else:
            if hi >= cur_sl:
                return _close(d, i, j, sig, entry, cur_sl, qty, qty_risk, qty_cap, dist, target, "SL")
            if lo <= target:
                return _close(d, i, j, sig, entry, target, qty, qty_risk, qty_cap, dist, target, "TARGET")
            if lo <= entry - dist:
                reached_1r = True
            allow = (S.TRAIL_MODE == "full" and cl < entry) or (S.TRAIL_MODE == "after_1R" and reached_1r)
            if allow:
                cur_sl = min(cur_sl, hi)
    return _close(d, i, len(d) - 1, sig, entry, float(d.iloc[-1]["close"]), qty, qty_risk, qty_cap, dist, target, "EOD")


def _close(d, sig_i, exit_i, sig, entry, exit_p, qty, qty_risk, qty_cap, dist, target, reason):
    gross = (exit_p - entry) * qty * (1 if sig["dir"] == "LONG" else -1)
    ch = S.compute_charges(entry, exit_p, qty)
    r_mult = (exit_p - entry) / dist * (1 if sig["dir"] == "LONG" else -1)
    return {**sig, "entry": entry, "entry_time": str(d.iloc[sig_i + 1]["_dt"]),
            "sl_dist": dist, "qty": qty, "qty_risk": qty_risk, "qty_cap": qty_cap,
            "target": target, "exit": exit_p, "exit_time": str(d.iloc[exit_i]["_dt"]),
            "reason": reason, "gross": gross, "charges": ch, "net": gross - ch, "R": r_mult}


def replay_symbol(sym, day):
    df = S.fetch_history(sym, S.INTERVAL, 3)
    if df is None:
        return None
    d = prep(df, day)
    if d is None:
        return None
    for i in range(2, len(d)):
        sig = signal_at(d, i)
        if sig:
            tr = simulate(d, i, sig)
            if tr:
                tr["symbol"] = sym
                return tr
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_today_ist())
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()

    print("=" * 110)
    print(f"  VERIFICATION REPORT — {args.date}   interval={S.INTERVAL}")
    print(f"  Config: single={S.ENABLE_SINGLE} double={S.ENABLE_DOUBLE}  VWAP={S.USE_VWAP}  "
          f"ADX>={S.ADX_THRESHOLD}  target={S.TARGET_RR}R  trail={S.TRAIL_MODE}")
    print(f"  Sizing: risk_budget=Rs.{S.BALANCE*S.MAX_LOSS_PCT/100:,.0f} "
          f"(={S.BALANCE:,.0f} x {S.MAX_LOSS_PCT}%)   notional_cap=Rs.{S.CAPITAL_PER_TRADE:,.0f}")
    print("=" * 110)

    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        stocks = S.fetch_intraday_boost()
        if not stocks:
            print("No TradeFinder stocks (JWT expired?). Use --symbols A,B,C to verify a manual list.")
            sys.exit(1)
        symbols = [s["symbol"] for s in stocks[:args.top]]

    trades = []
    for sym in symbols:
        tr = replay_symbol(sym, args.date)
        if tr:
            trades.append(tr)
    trades.sort(key=lambda x: x["sig_time"])

    if not trades:
        print("No trades triggered with the current config.")
        return

    # Detailed per-trade block (verify each against the chart)
    for k, t in enumerate(trades, 1):
        print(f"\n[{k}] {t['symbol']}  {t['dir']} ({t['kind']})   ADX={t['adx']:.1f}  "
              f"VWAP={t['vwap']:.2f}" if t["vwap"] else f"\n[{k}] {t['symbol']}  {t['dir']} ({t['kind']})  ADX={t['adx']:.1f}")
        print(f"     signal candle {_t(t['sig_time'])}  O={t['sig_o']:.2f} H={t['sig_h']:.2f} "
              f"L={t['sig_l']:.2f} C={t['sig_c']:.2f}")
        print(f"     ENTRY  {t['entry']:.2f}  @ {_t(t['entry_time'])} (next-bar open)")
        print(f"     SL     {t['sl']:.2f}  [{t['sl_src']}]   distance = {t['sl_dist']:.2f} pts")
        print(f"     QTY    {t['qty']}   = min( risk {int(S.BALANCE*S.MAX_LOSS_PCT/100)}/{t['sl_dist']:.2f}={t['qty_risk']}, "
              f"cap {int(S.CAPITAL_PER_TRADE)}/{t['entry']:.2f}={t['qty_cap']} )")
        print(f"     TARGET {t['target']:.2f}  (entry {'+' if t['dir']=='LONG' else '-'} {S.TARGET_RR}R x {t['sl_dist']:.2f})")
        print(f"     EXIT   {t['exit']:.2f}  @ {_t(t['exit_time'])}  [{t['reason']}]   "
              f"R={t['R']:+.2f}  gross={t['gross']:+.0f}  chrg={t['charges']:.0f}  NET={t['net']:+.0f}")

    # Compact summary table
    print("\n" + "=" * 110)
    print(f"{'#':>2} {'SYMBOL':<11}{'DIR':<6}{'SIGtime':<8}{'ENTRY':>9}{'SL':>9}{'TARGET':>9}"
          f"{'EXIT':>9}{'QTY':>6}{'R':>6}{'NET':>9}  REASON")
    print("-" * 110)
    tn = tg = 0.0
    wins = 0
    for k, t in enumerate(trades, 1):
        tg += t["gross"]; tn += t["net"]; wins += 1 if t["net"] > 0 else 0
        print(f"{k:>2} {t['symbol']:<11}{t['dir']:<6}{_t(t['sig_time']):<8}{t['entry']:>9.2f}"
              f"{t['sl']:>9.2f}{t['target']:>9.2f}{t['exit']:>9.2f}{t['qty']:>6}{t['R']:>+6.2f}"
              f"{t['net']:>+9.0f}  {t['reason']}")
    n = len(trades)
    print("-" * 110)
    print(f"Trades={n}  Wins={wins} ({100*wins/n:.0f}%)  Gross=Rs.{tg:+,.0f}  "
          f"Charges=Rs.{sum(t['charges'] for t in trades):,.0f}  NET=Rs.{tn:+,.0f}")

    # CSV for side-by-side chart verification
    csv_path = f"breakout_verify_{args.date}.csv"
    cols = ["symbol", "dir", "kind", "adx", "sig_time", "sig_o", "sig_h", "sig_l", "sig_c",
            "entry", "entry_time", "sl", "sl_src", "sl_dist", "qty", "qty_risk", "qty_cap",
            "target", "exit", "exit_time", "reason", "R", "gross", "charges", "net"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print(f"\nCSV written: {csv_path}  (open alongside your TradingView chart to verify each row)")
    print("=" * 110)


if __name__ == "__main__":
    main()
