#!/usr/bin/env python3
"""
Tests KAMA as a trade-MANAGEMENT tool (not a pre-entry filter, which already
failed in kama_filter_study.py) for the CPR breakout setup.

Rationale: the pre-entry KAMA filter failed because ~91% of entries fire on a
day's first bar - a multi-day KAMA at that moment reflects yesterday's stale
trend, not today's fresh gap. But a SAME-DAY-ONLY KAMA, reset at each day's
open, has real information once the trade has been running a while: it warms
up as the session progresses and can flag an early reversal well before the
trade would hit its fixed 1% stop or ride passively to EOD.

Same entries as the original backtest (reused from the trades CSV - same
signal, same side, same size). Only the EXIT rule changes: exit at the
earliest of {1% stop, KAMA reversal (once KAMA has enough same-day bars to be
valid), EOD}, instead of {1% stop, EOD} only. Compares net P&L before/after
on the full trade set and on the already-identified "sweet spot" subset.

Run: uv run python backtesting/cpr_breakout_equity/kama_trail_exit_study.py
"""
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openalgo import ta

_dir = Path(__file__).resolve().parent
CACHE_DIR = _dir / "results" / "raw_cache"
KAMA_LEN, KAMA_FAST, KAMA_SLOW = 7, 2, 15  # shorter length - needs to warm up within one session
MIN_BARS_FOR_KAMA = KAMA_LEN + 2
EOD_CUTOFF = datetime.strptime("15:20", "%H:%M").time()

BROKERAGE_PER_ORDER = 20
TXN_PCT = 0.0225 / 100
STT_INTRADAY_PCT = 0.025 / 100
GST_PCT = 0.18


def leg_charges(entry: float, exit_: float, qty: int, side: str) -> float:
    turnover = (entry + exit_) * qty
    brokerage = BROKERAGE_PER_ORDER * 2
    txn = TXN_PCT * turnover
    stt = STT_INTRADAY_PCT * (exit_ if side == "long" else entry) * qty
    gst = GST_PCT * (brokerage + txn)
    return brokerage + txn + stt + gst


def simulate_kama_trail_exit(day_bars: pd.DataFrame, entry_idx: int, entry_price: float, side: str, qty: int):
    sl_price = entry_price * 0.99 if side == "long" else entry_price * 1.01

    closes = day_bars["close"].values
    kama = ta.kama(closes, length=KAMA_LEN, fast_length=KAMA_FAST, slow_length=KAMA_SLOW)

    for i in range(entry_idx + 1, len(day_bars)):
        row = day_bars.iloc[i]
        ts = day_bars.index[i]

        if side == "long" and row["low"] <= sl_price:
            return sl_price, ts, "sl"
        if side == "short" and row["high"] >= sl_price:
            return sl_price, ts, "sl"

        if i >= MIN_BARS_FOR_KAMA and not np.isnan(kama[i]):
            if side == "long" and row["close"] < kama[i]:
                return row["close"], ts, "kama_reversal"
            if side == "short" and row["close"] > kama[i]:
                return row["close"], ts, "kama_reversal"

        if ts.time() >= EOD_CUTOFF:
            return row["close"], ts, "eod"

    last = day_bars.iloc[-1]
    return last["close"], day_bars.index[-1], "eod_lastbar"


def main():
    trades = pd.read_csv(_dir / "results" / "cpr_breakout_trades.csv", parse_dates=["entry_ts", "exit_ts"])
    trades["date"] = pd.to_datetime(trades["date"]).dt.date

    new_rows = []
    sym_cache: dict[str, pd.DataFrame] = {}

    for _, tr in trades.iterrows():
        sym = tr["symbol"]
        if sym not in sym_cache:
            path = CACHE_DIR / f"{sym}_5m.parquet"
            sym_cache[sym] = pd.read_parquet(path) if path.exists() else None
        full_df = sym_cache[sym]
        if full_df is None:
            continue

        day_bars = full_df[full_df.index.date == tr["date"]]
        if day_bars.empty:
            continue
        if tr["entry_ts"] not in day_bars.index:
            continue
        entry_idx = day_bars.index.get_loc(tr["entry_ts"])

        exit_price, exit_ts, reason = simulate_kama_trail_exit(
            day_bars, entry_idx, tr["entry"], tr["side"], tr["qty"]
        )
        gross = (
            (exit_price - tr["entry"]) * tr["qty"]
            if tr["side"] == "long"
            else (tr["entry"] - exit_price) * tr["qty"]
        )
        ch = leg_charges(tr["entry"], exit_price, tr["qty"], tr["side"])
        net = gross - ch

        new_rows.append({
            "date": tr["date"], "symbol": sym, "side": tr["side"], "qty": tr["qty"],
            "entry": tr["entry"], "new_exit": exit_price, "new_exit_reason": reason,
            "width_pct": tr["width_pct"], "breakout_strength_pct": tr["breakout_strength_pct"],
            "new_net": net, "old_net": tr["net"], "old_exit_reason": tr["exit_reason"],
        })

    df = pd.DataFrame(new_rows)
    df.to_csv(_dir / "results" / "cpr_breakout_kama_trail.csv", index=False)

    def report(label, sel):
        if len(sel) == 0:
            print(f"{label}: 0 trades")
            return
        print(f"{label}: {len(sel)} trades, win_rate={100*(sel['new_net']>0).mean():.1f}%, "
              f"net=Rs{sel['new_net'].sum():,.0f}, avg=Rs{sel['new_net'].mean():,.0f}")

    print("=" * 90)
    print(f"Original (fixed 1% SL + EOD only): {len(trades)} trades, "
          f"win_rate={100*(trades['net']>0).mean():.1f}%, net=Rs{trades['net'].sum():,.0f}")
    report("With KAMA-trail exit added", df)
    print()
    print("New exit reason breakdown:")
    print(df["new_exit_reason"].value_counts())

    print()
    sweet = df[(df["breakout_strength_pct"] > 0.5) & (df["breakout_strength_pct"] <= 1.0)
               & (df["width_pct"] < 0.3)]
    print(f"Sweet-spot ORIGINAL exit: net=Rs{sweet['old_net'].sum():,.0f}")
    report("Sweet-spot WITH KAMA-trail exit", sweet)


if __name__ == "__main__":
    main()
