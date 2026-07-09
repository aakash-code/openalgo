#!/usr/bin/env python3
"""
Directional trend signals on NIFTY SPOT — all years (2022 -> Jun 2026)
=====================================================================
Tests whether a trend signal actually predicts DIRECTION on NIFTY, across
regimes (2022 bear, 2023 range, 2024-25 bull, 2026 mixed) — the cheap, honest
pre-check before committing to directional option selling.

Three signals, all causal (signal at bar close -> position next bar, no look-ahead):
  1. Daily Supertrend(10, 3)          [positional, hold to flip]
  2. Daily EMA 20/50 crossover        [positional, hold to flip]
  3. Intraday 3m EMA-stack (8>21>50)  [intraday, flat overnight]

P&L is measured in NIFTY POINTS captured by going long in uptrends / short in
downtrends. Benchmark = Buy & Hold (always long). Rupees ~ points x 75 (lot).

Run: uv run python backtesting/aakash_signal_spread/directional_spot_test.py
"""
from __future__ import annotations

import os
from datetime import datetime, time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")
LOT = 75  # representative, for rupee intuition only


def load_1m(conn):
    df = conn.execute("""
        SELECT timestamp, open, high, low, close FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m' ORDER BY timestamp
    """).df()
    df["dt"] = (pd.to_datetime(df["timestamp"], unit="s", utc=True)
                  .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    return df.set_index("dt").drop(columns=["timestamp"]).between_time("09:15", "15:29")


def supertrend(h, l, c, period=10, mult=3.0):
    n = len(c); h, l, c = map(lambda x: np.asarray(x, float), (h, l, c))
    tr = np.empty(n); tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    atr = np.zeros(n)
    if n >= period:
        atr[period-1] = tr[:period].mean()
        for i in range(period, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    hl2 = (h + l) / 2.0
    up, dn = hl2 + mult * atr, hl2 - mult * atr
    fu, fl = up.copy(), dn.copy()
    dir_ = np.ones(n, int)
    for i in range(1, n):
        fu[i] = min(up[i], fu[i-1]) if c[i-1] <= fu[i-1] else up[i]
        fl[i] = max(dn[i], fl[i-1]) if c[i-1] >= fl[i-1] else dn[i]
        dir_[i] = 1 if c[i] > fu[i-1] else (-1 if c[i] < fl[i-1] else dir_[i-1])
    return dir_


def positional_pnl(close, pos):
    """pos[i] decided at close[i] -> applies to return close[i+1]-close[i] (no look-ahead)."""
    close = np.asarray(close, float)
    ret = np.diff(close)
    p = np.asarray(pos, float)[:-1]      # shift: yesterday's signal drives today's move
    pnl = p * ret
    flips = int((np.diff(np.asarray(pos)) != 0).sum())
    return pnl, flips


def year_table(dates, pnl, label):
    s = pd.Series(pnl, index=pd.to_datetime(dates[1:]))
    by = s.groupby(s.index.year).sum()
    eq = s.cumsum(); dd = (eq - eq.cummax()).min()
    return by, float(s.sum()), float(dd)


def main():
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)
    m1 = load_1m(conn)
    conn.close()

    # Clean daily bars from 1m (complete through Jun 2026).
    daily = (m1.resample("1D").agg(open=("open", "first"), high=("high", "max"),
             low=("low", "min"), close=("close", "last")).dropna())
    dts = daily.index
    c = daily["close"].values

    # --- 1) Supertrend(10,3) daily ---
    st = supertrend(daily["high"], daily["low"], daily["close"], 10, 3.0)
    st_pnl, st_flips = positional_pnl(c, st)

    # --- 2) EMA 20/50 daily ---
    e20 = daily["close"].ewm(span=20, adjust=False).mean()
    e50 = daily["close"].ewm(span=50, adjust=False).mean()
    ema_pos = np.where(e20 > e50, 1, -1)
    ema_pnl, ema_flips = positional_pnl(c, ema_pos)

    # --- Benchmark: Buy & Hold (always long) ---
    bh_pnl, _ = positional_pnl(c, np.ones(len(c), int))

    # --- 3) Intraday 3m EMA-stack (flat overnight) ---
    b3 = (m1.resample("3min", closed="left", label="left")
            .agg(open=("open", "first"), high=("high", "max"),
                 low=("low", "min"), close=("close", "last")).dropna()
            .between_time("09:15", "15:25"))
    for ln in (8, 21, 50):
        b3[f"e{ln}"] = b3["close"].ewm(span=ln, adjust=False).mean()
    stack = np.where((b3.e8 > b3.e21) & (b3.e21 > b3.e50), 1,
             np.where((b3.e8 < b3.e21) & (b3.e21 < b3.e50), -1, 0))
    b3 = b3.assign(pos=stack)
    b3["date"] = b3.index.date
    b3["ret"] = b3.groupby("date")["close"].diff()      # intraday returns only
    b3["pos_prev"] = b3.groupby("date")["pos"].shift()   # causal: prior bar's stack
    b3["pnl"] = (b3["pos_prev"].fillna(0) * b3["ret"].fillna(0))
    intr = b3.groupby(b3.index.year)["pnl"].sum()
    intr_total = float(b3["pnl"].sum())

    # ---- Report (optional window via START/END; default = last 6 months) ----
    W0 = pd.Timestamp(os.getenv("START", "2025-12-01"))
    W1 = pd.Timestamp(os.getenv("END", "2026-06-30"))
    dpf = pd.DataFrame({"BuyHold": bh_pnl, "Supertrend": st_pnl, "EMA20/50": ema_pnl},
                       index=pd.to_datetime(dts[1:]))
    dwin = dpf[(dpf.index >= W0) & (dpf.index <= W1)]
    iwin = b3[(b3.index >= W0) & (b3.index <= W1)]
    intr_m = iwin.groupby(iwin.index.strftime("%Y-%m"))["pnl"].sum()
    dwin_m = dwin.groupby(dwin.index.strftime("%Y-%m")).sum()
    months = sorted(set(dwin_m.index) | set(intr_m.index))
    flips_intra = int((iwin["pos"].diff().fillna(0) != 0).sum())
    n_days_w = iwin["date"].nunique()

    print("=" * 80)
    print(f"DIRECTIONAL TREND on NIFTY SPOT — points captured  | window {W0.date()} .. {W1.date()}")
    print("=" * 80)
    print(f"{'Month':<9}{'BuyHold':>11}{'Supertrend':>13}{'EMA20/50':>12}{'Intraday3m':>13}")
    print("-" * 80)
    for mth in months:
        bh = dwin_m["BuyHold"].get(mth, 0); st = dwin_m["Supertrend"].get(mth, 0)
        em = dwin_m["EMA20/50"].get(mth, 0); it = intr_m.get(mth, 0)
        print(f"{mth:<9}{bh:>11,.0f}{st:>13,.0f}{em:>12,.0f}{it:>13,.0f}")
    print("-" * 80)
    print(f"{'TOTAL':<9}{dwin['BuyHold'].sum():>11,.0f}{dwin['Supertrend'].sum():>13,.0f}"
          f"{dwin['EMA20/50'].sum():>12,.0f}{iwin['pnl'].sum():>13,.0f}")
    print(f"\nIntraday3m in window: {flips_intra} position changes over {n_days_w} days "
          f"= {flips_intra/max(1,n_days_w):.1f} trades/day")
    print(f"Points x {LOT} = rupees/lot. Intraday3m window total = "
          f"Rs {iwin['pnl'].sum()*LOT:,.0f}/lot (GROSS, before costs).")
    cost_pts = flips_intra * (LOT * 0 + 1) * 0  # placeholder; costs added per-test
    print("NOTE: Intraday3m is GROSS spot points (no brokerage/slippage). With "
          f"{flips_intra/max(1,n_days_w):.1f} trades/day, costs matter — next test adds them.")


if __name__ == "__main__":
    main()
