#!/usr/bin/env python3
"""
intraday_scanner_backtest.py
─────────────────────────────────────────────────────────────────────────────
Walk-forward backtest of the Intraday Squeeze Scanner.

For every trading day in the lookback window, this script:
  1. Simulates the scanner running AFTER market close (using data ≤ scan_date)
  2. Picks the Top-N squeeze candidates by composite score
  3. Measures how those picks actually performed the NEXT trading day:
       • Gap %         → (next_open  - prev_close) / prev_close × 100
       • Close %       → (next_close - prev_close) / prev_close × 100
       • Max Gain %    → (next_high  - prev_close) / prev_close × 100
       • Max Loss %    → (next_low   - prev_close) / prev_close × 100
       • Outcome       → TARGET (+2%) | STOP (-1%) | BOTH_HIT | EXIT_CLOSE

Usage:
    python intraday_scanner_backtest.py               # last 2 years, top-5
    python intraday_scanner_backtest.py --days 365    # last 1 year only
    python intraday_scanner_backtest.py --top 10      # top-10 picks per day
"""

import argparse
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import warnings

from database.historify_db import get_connection
from utils.volatility import compute_ohlc_volatility
from utils.ml_volatility import compute_lwma, prepare_ml_dataset, train_nnls_model

warnings.filterwarnings("ignore")

# ─── Default Config ────────────────────────────────────────────────────────────
MIN_DAYS   = 30     # min candles needed to score a symbol
TOP_N      = 5      # picks per scan day  (overridable via --top)
TARGET_PCT = 2.0    # % profit target
STOP_PCT   = -1.0   # % stop-loss
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_all_daily_data() -> dict:
    """
    Load all NSE Daily (interval='D') OHLCV rows from Historify DB.
    Returns a dict:  symbol → DataFrame[open, high, low, close, volume]
                                        indexed by datetime.date
    """
    print("📦 Loading all Daily NSE data from Historify DB …", flush=True)
    with get_connection() as conn:
        raw = conn.execute("""
            SELECT symbol, timestamp, open, high, low, close, volume
            FROM market_data
            WHERE exchange = 'NSE' AND interval = 'D'
            ORDER BY symbol, timestamp
        """).fetchdf()

    if raw.empty:
        sys.exit("❌  No Daily (D) data found in Historify DB. Download data first.")

    raw["date"] = pd.to_datetime(raw["timestamp"], unit="s").dt.date

    data: dict = {}
    for sym, grp in raw.groupby("symbol"):
        g = (grp
             .sort_values("date")
             .drop_duplicates("date")
             .set_index("date")
             [["open", "high", "low", "close", "volume"]])
        data[sym] = g

    total_rows = sum(len(v) for v in data.values())
    print(f"✅  Loaded {len(data):,} symbols · {total_rows:,} daily rows.\n", flush=True)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# SCANNER LOGIC  (mirrors intraday_setup_scanner.py exactly)
# ══════════════════════════════════════════════════════════════════════════════

def score_symbol(df_slice: pd.DataFrame) -> dict | None:
    """
    Run the full squeeze-scanner logic on a historical slice.
    Returns a dict with {price, squeeze, trend, exp_pot, score}
    or None if the symbol should be skipped.
    """
    if len(df_slice) < MIN_DAYS:
        return None
    try:
        df_vol = compute_ohlc_volatility(df_slice)

        current_yz = df_vol["vol_yz_ann"].iloc[-1]
        avg_yz     = df_vol["vol_yz_ann"].rolling(10).mean().iloc[-1]
        squeeze    = current_yz / avg_yz if avg_yz > 0 else 1.0

        if squeeze >= 1.0:          # only compressed setups
            return None

        X, y, _ = prepare_ml_dataset(df_vol, horizon=1, window=10)
        if len(X) < 10:
            return None

        weights, _ = train_nnls_model(X.values, y.values)
        latest_feat = [
            compute_lwma(df_vol[est], 10).iloc[-1]
            for est in ["vol_yz_ann", "vol_rs_ann", "vol_gk_ann", "vol_parkinson_ann"]
        ]
        pred_vol = np.dot(latest_feat, weights)
        exp_pot  = pred_vol / current_yz if current_yz > 0 else 1.0

        volm_spike = df_slice["volume"].iloc[-1] / df_slice["volume"].tail(5).mean()
        sma20      = df_slice["close"].rolling(20).mean().iloc[-1]
        curr_price = df_slice["close"].iloc[-1]
        trend      = "BULL" if curr_price > sma20 else "BEAR"
        score      = exp_pot / squeeze * volm_spike

        return {
            "price":   round(curr_price, 2),
            "squeeze": round(squeeze,    4),
            "trend":   trend,
            "exp_pot": round(exp_pot,    4),
            "score":   round(score,      4),
        }
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# NEXT-DAY PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════

def next_day_perf(df_sym: pd.DataFrame, scan_date: date, prev_close: float) -> dict | None:
    """
    Given the full symbol DataFrame and the scan date, find the very next
    trading day and compute all performance metrics.
    """
    future = df_sym[df_sym.index > scan_date]
    if future.empty:
        return None

    r         = future.iloc[0]
    next_date = future.index[0]

    gap_pct   = (r["open"]  - prev_close) / prev_close * 100
    high_pct  = (r["high"]  - prev_close) / prev_close * 100
    low_pct   = (r["low"]   - prev_close) / prev_close * 100
    close_pct = (r["close"] - prev_close) / prev_close * 100

    target_px = prev_close * (1 + TARGET_PCT / 100)
    stop_px   = prev_close * (1 + STOP_PCT   / 100)

    t_hit = r["high"] >= target_px
    s_hit = r["low"]  <= stop_px

    # Conservative: if both target & stop hit on same day → assume stop filled first
    if t_hit and s_hit:
        outcome, trade_pct = "BOTH_HIT", STOP_PCT
    elif t_hit:
        outcome, trade_pct = "TARGET",     TARGET_PCT
    elif s_hit:
        outcome, trade_pct = "STOP",       STOP_PCT
    else:
        outcome, trade_pct = "EXIT_CLOSE", round(close_pct, 2)

    return {
        "next_date":  next_date,
        "next_open":  round(r["open"],  2),
        "next_high":  round(r["high"],  2),
        "next_low":   round(r["low"],   2),
        "next_close": round(r["close"], 2),
        "gap_pct":    round(gap_pct,   2),
        "high_pct":   round(high_pct,  2),
        "low_pct":    round(low_pct,   2),
        "close_pct":  round(close_pct, 2),
        "target_hit": t_hit,
        "stop_hit":   s_hit,
        "outcome":    outcome,
        "trade_pct":  round(trade_pct, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(lookback_days: int = 730, top_n: int = TOP_N):
    data = load_all_daily_data()

    # All unique trading dates across every symbol, sorted ascending
    all_dates = sorted({d for df in data.values() for d in df.index})
    cutoff    = date.today() - timedelta(days=lookback_days)

    # Exclude the very last date (no next-day candle to measure performance)
    scan_dates = [d for d in all_dates if d >= cutoff][:-1]

    if not scan_dates:
        sys.exit("❌  No trading dates in the selected lookback window.")

    print(f"📅  Walk-forward dates : {len(scan_dates)}"
          f"  ({scan_dates[0]} → {scan_dates[-1]})")
    print(f"🔍  Top {top_n} picks/day | "
          f"Target +{TARGET_PCT}% | Stop {STOP_PCT}% | Min history {MIN_DAYS} days\n")

    records = []
    n = len(scan_dates)

    for i, scan_date in enumerate(scan_dates):
        pct = (i + 1) / n * 100
        print(f"\r⏳  {i+1}/{n} ({pct:5.1f}%)  [{scan_date}]", end="", flush=True)

        # ── Score every symbol as of scan_date ──────────────────────────────
        candidates = []
        for sym, df_all in data.items():
            df_slice = df_all[df_all.index <= scan_date]
            s = score_symbol(df_slice)
            if s:
                candidates.append((sym, s))

        if not candidates:
            continue

        # Top-N by composite score
        candidates.sort(key=lambda x: x[1]["score"], reverse=True)
        picks = candidates[:top_n]

        # ── Measure next-day performance for each pick ───────────────────────
        for sym, info in picks:
            if sym not in data:
                continue
            perf = next_day_perf(data[sym], scan_date, info["price"])
            if perf is None:
                continue
            records.append({
                "scan_date": scan_date,
                "symbol":    sym,
                "price":     info["price"],
                "squeeze":   info["squeeze"],
                "trend":     info["trend"],
                "exp_pot":   info["exp_pot"],
                "score":     info["score"],
                **perf,
            })

    print()  # newline after progress bar

    if not records:
        print("❌  No records generated. Check your data coverage.")
        return

    df_out = pd.DataFrame(records)

    # ── Save raw results ─────────────────────────────────────────────────────
    out_csv = "intraday_scanner_backtest_results.csv"
    df_out.to_csv(out_csv, index=False)
    print(f"\n💾  Saved {len(df_out):,} pick records → {out_csv}\n")

    # ══════════════════════════════════════════════════════════════════════════
    # SUMMARY REPORT
    # ══════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("📊  INTRADAY SQUEEZE SCANNER — BACKTEST SUMMARY")
    print("=" * 70)

    total  = len(df_out)
    target = (df_out["outcome"] == "TARGET").sum()
    stop   = (df_out["outcome"] == "STOP").sum()
    both   = (df_out["outcome"] == "BOTH_HIT").sum()
    exits  = (df_out["outcome"] == "EXIT_CLOSE").sum()

    win_rate  = target / total * 100
    stop_rate = (stop + both) / total * 100

    print(f"  Scan days analyzed    : {len(scan_dates)}")
    print(f"  Total picks analyzed  : {total:,}")
    print(f"  Unique symbols seen   : {df_out['symbol'].nunique()}")
    print()
    print(f"── OUTCOMES ──────────────────────────────────────────────────────")
    print(f"  🎯 Target Hit  (≥ +{TARGET_PCT}%)     : {target:5,d}  ({win_rate:.1f}%)")
    print(f"  🛑 Stop Hit    (≤ {STOP_PCT}%)      : {stop:5,d}  ({stop/total*100:.1f}%)")
    print(f"  ↕️  Both Hit (conservative→Stop) : {both:5,d}  ({both/total*100:.1f}%)")
    print(f"  📉 Closed at market end          : {exits:5,d}  ({exits/total*100:.1f}%)")
    print()

    avg_trade    = df_out["trade_pct"].mean()
    med_trade    = df_out["trade_pct"].median()
    avg_close    = df_out["close_pct"].mean()
    avg_high     = df_out["high_pct"].mean()
    avg_low      = df_out["low_pct"].mean()
    avg_gap      = df_out["gap_pct"].mean()

    print(f"── RETURNS (vs prev close) ───────────────────────────────────────")
    print(f"  Avg trade P&L         : {avg_trade:+.2f}%")
    print(f"  Median trade P&L      : {med_trade:+.2f}%")
    print(f"  Avg next-day gap      : {avg_gap:+.2f}%")
    print(f"  Avg next-day close ∆  : {avg_close:+.2f}%")
    print(f"  Avg max intraday gain : {avg_high:+.2f}%")
    print(f"  Avg max intraday loss : {avg_low:+.2f}%")
    print()

    # Expectancy
    exp_val = (win_rate/100 * TARGET_PCT) + (stop_rate/100 * STOP_PCT) + \
              (exits/total * avg_close)
    print(f"  💰 Expectancy per trade : {exp_val:+.3f}%")
    verdict = "✅ POSITIVE EDGE" if exp_val > 0 else "❌ NEGATIVE EDGE"
    print(f"  Verdict               : {verdict}")
    print()

    # ── BULL vs BEAR split ───────────────────────────────────────────────────
    print(f"── TREND SPLIT ───────────────────────────────────────────────────")
    for trend, emoji in [("BULL", "🟢"), ("BEAR", "🔴")]:
        sub = df_out[df_out["trend"] == trend]
        if sub.empty:
            continue
        tw = (sub["outcome"] == "TARGET").sum()
        print(f"  {emoji} {trend:4s}: {len(sub):4,d} picks | "
              f"Win {tw/len(sub)*100:4.1f}% | "
              f"Avg {sub['trade_pct'].mean():+.2f}% | "
              f"Median {sub['trade_pct'].median():+.2f}%")
    print()

    # ── Monthly breakdown ────────────────────────────────────────────────────
    df_out["month"] = pd.to_datetime(df_out["scan_date"]).dt.to_period("M")
    monthly = (df_out.groupby("month")
               .agg(picks=("trade_pct", "count"),
                    win_rate=("target_hit", lambda x: x.mean()*100),
                    avg_return=("trade_pct", "mean"))
               .reset_index())
    print(f"── MONTHLY PERFORMANCE ───────────────────────────────────────────")
    print(f"  {'Month':<10} {'Picks':>6} {'Win%':>7} {'Avg Return':>12}")
    print(f"  {'-'*10} {'-'*6} {'-'*7} {'-'*12}")
    for _, row in monthly.iterrows():
        bar = "█" * int(max(0, row['avg_return']) * 5)
        print(f"  {str(row['month']):<10} {int(row['picks']):>6} "
              f"{row['win_rate']:>6.1f}% {row['avg_return']:>+10.2f}%  {bar}")
    print()

    # ── Top 10 symbols by avg trade return (min 3 picks) ────────────────────
    sym_stats = (df_out.groupby("symbol")["trade_pct"]
                 .agg(picks="count", avg_return="mean", win_rate=lambda x: (x >= TARGET_PCT).mean()*100)
                 .query("picks >= 3")
                 .sort_values("avg_return", ascending=False))

    print(f"── TOP 10 SYMBOLS (avg return, min 3 picks) ──────────────────────")
    print(f"  {'Symbol':<14} {'Picks':>5} {'Win%':>7} {'Avg Return':>12}")
    print(f"  {'-'*14} {'-'*5} {'-'*7} {'-'*12}")
    for sym, row in sym_stats.head(10).iterrows():
        print(f"  {sym:<14} {int(row['picks']):>5} "
              f"{row['win_rate']:>6.1f}% {row['avg_return']:>+10.2f}%")
    print()

    print(f"── WORST 5 SYMBOLS (avg return, min 3 picks) ─────────────────────")
    print(f"  {'Symbol':<14} {'Picks':>5} {'Win%':>7} {'Avg Return':>12}")
    print(f"  {'-'*14} {'-'*5} {'-'*7} {'-'*12}")
    for sym, row in sym_stats.tail(5).iterrows():
        print(f"  {sym:<14} {int(row['picks']):>5} "
              f"{row['win_rate']:>6.1f}% {row['avg_return']:>+10.2f}%")
    print()

    # ── Score percentile analysis ────────────────────────────────────────────
    df_out["score_bucket"] = pd.qcut(df_out["score"], q=4,
                                     labels=["Low", "Mid-Low", "Mid-High", "High"])
    score_grp = (df_out.groupby("score_bucket", observed=True)
                 .agg(picks=("trade_pct", "count"),
                      win_rate=("target_hit", lambda x: x.mean()*100),
                      avg_return=("trade_pct", "mean")))
    print(f"── SCORE QUARTILE ANALYSIS ───────────────────────────────────────")
    print(f"  {'Score Q':<10} {'Picks':>6} {'Win%':>7} {'Avg Return':>12}")
    print(f"  {'-'*10} {'-'*6} {'-'*7} {'-'*12}")
    for bucket, row in score_grp.iterrows():
        print(f"  {str(bucket):<10} {int(row['picks']):>6} "
              f"{row['win_rate']:>6.1f}% {row['avg_return']:>+10.2f}%")

    print()
    print("=" * 70)
    print(f"Full results saved to: {out_csv}")
    print("=" * 70)


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Walk-forward backtest of the Intraday Squeeze Scanner"
    )
    parser.add_argument(
        "--days", type=int, default=730,
        help="Lookback in calendar days (default: 730 = ~2 years)"
    )
    parser.add_argument(
        "--top", type=int, default=5,
        help="Top-N picks per scan day (default: 5)"
    )
    args = parser.parse_args()
    run_backtest(lookback_days=args.days, top_n=args.top)
