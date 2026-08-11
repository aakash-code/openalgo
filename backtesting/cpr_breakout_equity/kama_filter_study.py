#!/usr/bin/env python3
"""
Tests whether adding a KAMA-based trend/chop filter improves the CPR breakout
setup studied in cpr_breakout_backtest.py. Reuses the cached 5m data (no new
broker calls) and the existing trades CSV.

For each trade: computes KAMA(14,2,30) CONTINUOUSLY across the symbol's whole
cached date range (not reset daily - most entries fire on a day's very first
bar, so a same-day-only KAMA would have no warmup history at entry) and the
matching Efficiency Ratio (KAMA's own internal trend-vs-chop measure), both
evaluated causally up to and including the entry bar. Checks whether the
trade's direction agrees with KAMA's slope at entry, and whether ER was above
a "trending enough to trust" threshold.

Run: uv run python backtesting/cpr_breakout_equity/kama_filter_study.py
"""
from pathlib import Path

import numpy as np
import pandas as pd
from openalgo import ta

_dir = Path(__file__).resolve().parent
CACHE_DIR = _dir / "results" / "raw_cache"
KAMA_LEN, KAMA_FAST, KAMA_SLOW = 14, 2, 30
ER_LEN = 14
SLOPE_LOOKBACK = 3  # bars back to measure KAMA slope direction


def efficiency_ratio(close: np.ndarray, length: int) -> np.ndarray:
    er = np.full(len(close), np.nan)
    for i in range(length, len(close)):
        change = abs(close[i] - close[i - length])
        volatility = np.sum(np.abs(np.diff(close[i - length: i + 1])))
        er[i] = change / volatility if volatility != 0 else 0.0
    return er


def main():
    trades = pd.read_csv(_dir / "results" / "cpr_breakout_trades.csv", parse_dates=["entry_ts", "exit_ts"])
    trades["date"] = pd.to_datetime(trades["date"]).dt.date

    kama_slope_ok = []
    er_at_entry = []

    kama_cache: dict[str, pd.DataFrame] = {}

    for _, tr in trades.iterrows():
        sym = tr["symbol"]
        if sym not in kama_cache:
            path = CACHE_DIR / f"{sym}_5m.parquet"
            if not path.exists():
                kama_cache[sym] = None
            else:
                df = pd.read_parquet(path)
                k = ta.kama(df["close"].values, length=KAMA_LEN, fast_length=KAMA_FAST, slow_length=KAMA_SLOW)
                er = efficiency_ratio(df["close"].values, ER_LEN)
                df = df.assign(kama=k, er=er)
                kama_cache[sym] = df

        df = kama_cache[sym]
        if df is None or df.empty:
            kama_slope_ok.append(None)
            er_at_entry.append(None)
            continue

        entry_ts = tr["entry_ts"]
        if entry_ts not in df.index:
            pos = df.index.searchsorted(entry_ts, side="right") - 1
            if pos < 0:
                kama_slope_ok.append(None)
                er_at_entry.append(None)
                continue
        else:
            pos = df.index.get_loc(entry_ts)

        if pos < SLOPE_LOOKBACK or np.isnan(df["kama"].iloc[pos]) or np.isnan(df["kama"].iloc[pos - SLOPE_LOOKBACK]):
            kama_slope_ok.append(None)
            er_at_entry.append(None)
            continue

        slope = df["kama"].iloc[pos] - df["kama"].iloc[pos - SLOPE_LOOKBACK]
        agrees = (slope > 0) if tr["side"] == "long" else (slope < 0)
        kama_slope_ok.append(agrees)
        er_at_entry.append(df["er"].iloc[pos])

    trades["kama_agrees"] = kama_slope_ok
    trades["er_at_entry"] = er_at_entry
    trades.to_csv(_dir / "results" / "cpr_breakout_trades_with_kama.csv", index=False)

    def report(label, sel):
        if len(sel) == 0:
            print(f"{label}: 0 trades")
            return
        print(f"{label}: {len(sel)} trades, win_rate={100*(sel['net']>0).mean():.1f}%, "
              f"net=Rs{sel['net'].sum():,.0f}, avg=Rs{sel['net'].mean():,.0f}")

    print("=" * 90)
    report("ALL trades (baseline)", trades)

    valid = trades.dropna(subset=["kama_agrees"])
    report("KAMA slope AGREES with trade direction", valid[valid["kama_agrees"] == True])  # noqa: E712
    report("KAMA slope DISAGREES", valid[valid["kama_agrees"] == False])  # noqa: E712

    print()
    sweet = trades[(trades["breakout_strength_pct"] > 0.5) & (trades["breakout_strength_pct"] <= 1.0)
                   & (trades["width_pct"] < 0.3)]
    report("Sweet-spot (strength 0.5-1%, width<0.3%) baseline", sweet)
    sweet_valid = sweet.dropna(subset=["kama_agrees"])
    report("Sweet-spot + KAMA agrees", sweet_valid[sweet_valid["kama_agrees"] == True])  # noqa: E712

    print()
    for er_thresh in (0.2, 0.3, 0.4):
        er_sel = trades.dropna(subset=["er_at_entry"])
        er_sel = er_sel[er_sel["er_at_entry"] >= er_thresh]
        report(f"ER >= {er_thresh} (trending enough)", er_sel)

    print()
    sweet_er = sweet.dropna(subset=["er_at_entry"])
    for er_thresh in (0.2, 0.3):
        report(f"Sweet-spot + ER >= {er_thresh}", sweet_er[sweet_er["er_at_entry"] >= er_thresh])


if __name__ == "__main__":
    main()
