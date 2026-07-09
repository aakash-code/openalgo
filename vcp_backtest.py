#!/usr/bin/env python3
"""
vcp_backtest.py
═══════════════════════════════════════════════════════════════════════════════
VCP (Volumetric Candle Pair) Breakout Strategy — Walk-Forward Backtest

EXACT mirror of the live scanner (volumetricCandlePair.ts):
  • C1/C2 zone detection RESETS each trading day (one zone per day)
  • Breakout detection only happens on candles AFTER C2 within the same day
  • Long: close > zoneHigh + EMA(8)>EMA(21)>EMA(50)>EMA(100) + close>EMA(8) + buyVol>sellVol
  • Short: close < zoneLow + EMA(8)<EMA(21)<EMA(50)<EMA(100) + close<EMA(8) + sellVol>buyVol
  • Only FIRST breakout and FIRST breakdown per day

Data source: Historify DuckDB (3m candles computed from 1m)

Usage:
    python vcp_backtest.py                        # defaults (1 year)
    python vcp_backtest.py --days 730             # 2 years
    python vcp_backtest.py --target-pct 2 --sl-pct 1.5
    python vcp_backtest.py --csv results.csv
"""

import argparse
import sys
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database.historify_db import get_ohlcv

warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

INTERVAL        = "3m"
EXCHANGE        = "NSE"
MIN_CANDLES     = 100        # for EMA(100) warm-up
CSV_PATH        = "/Users/bond7/Downloads/Watchlist 1.csv"

# Risk management
TOTAL_CAPITAL   = 300_000
CAP_PER_TRADE   = 6_000
LEVERAGE        = 5
MAX_TRADES_DAY  = 50
BUYING_POWER    = CAP_PER_TRADE * LEVERAGE  # ₹30,000

DEFAULT_TARGET_PCT = 1.5
DEFAULT_STOP_PCT   = 1.0

# Market hours (IST) — 9:15 to 15:30
MARKET_OPEN_H, MARKET_OPEN_M   = 9, 15
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def load_watchlist():
    if not os.path.exists(CSV_PATH):
        sys.exit(f"❌ Watchlist not found: {CSV_PATH}")
    df = pd.read_csv(CSV_PATH)
    return list(df.itertuples(index=False, name=None))


def load_and_prepare(symbols):
    """Load 3m data + pre-compute EMAs + buy/sell volume for all symbols."""
    print("📦 Loading 3m data from Historify DB …", flush=True)
    data = {}
    skipped = 0

    for symbol, exchange in symbols:
        df = get_ohlcv(symbol=symbol, exchange=exchange, interval=INTERVAL)
        if df.empty or len(df) < MIN_CANDLES:
            skipped += 1
            continue

        df = df.copy()
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
        # IST offset: +5:30
        df["datetime_ist"] = df["datetime"] + pd.Timedelta(hours=5, minutes=30)
        df["date"] = df["datetime_ist"].dt.date
        df["hour"] = df["datetime_ist"].dt.hour
        df["minute"] = df["datetime_ist"].dt.minute
        df.sort_values("timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)

        # Filter to market hours only (9:15 - 15:30 IST)
        df["in_market"] = (
            ((df["hour"] > MARKET_OPEN_H) |
             ((df["hour"] == MARKET_OPEN_H) & (df["minute"] >= MARKET_OPEN_M)))
            &
            ((df["hour"] < MARKET_CLOSE_H) |
             ((df["hour"] == MARKET_CLOSE_H) & (df["minute"] <= MARKET_CLOSE_M)))
        )

        # Pre-compute EMAs on the FULL dataset (not per-day — matches live scanner)
        df["e8"]   = df["close"].ewm(span=8, adjust=False).mean()
        df["e21"]  = df["close"].ewm(span=21, adjust=False).mean()
        df["e50"]  = df["close"].ewm(span=50, adjust=False).mean()
        df["e100"] = df["close"].ewm(span=100, adjust=False).mean()

        # Buy/sell volume partitioning (wick-based, per candle)
        hl = (df["high"] - df["low"]).replace(0, np.nan)
        df["buy_v"]  = (df["volume"] * (df["close"] - df["low"]) / hl).fillna(df["volume"] * 0.5)
        df["sell_v"] = (df["volume"] * (df["high"] - df["close"]) / hl).fillna(df["volume"] * 0.5)

        # Candle direction
        df["is_green"] = df["close"] > df["open"]
        df["is_red"]   = df["close"] < df["open"]

        data[symbol] = df

    total_rows = sum(len(v) for v in data.values())
    print(f"✅ Loaded {len(data)} symbols · {total_rows:,} candles "
          f"(skipped {skipped} with < {MIN_CANDLES} candles)\n", flush=True)
    return data


# ═══════════════════════════════════════════════════════════════════════════════
# VCP SCANNER — EXACT MIRROR OF volumetricCandlePair.ts
# ═══════════════════════════════════════════════════════════════════════════════

def scan_symbol_day(df, day_indices):
    """
    Scan one day's market-hours candles for VCP signals.
    Mirrors the TypeScript indicator exactly:
      1. Find C1: first volume-spike green/red candle (vs prior bar)
      2. Find C2: next opposite-color volume-spike candle
      3. Zone = max(C1.high, C2.high) / min(C1.low, C2.low)
      4. After C2, look for breakout/breakdown with ALL filters

    Returns list of signal dicts (at most 2: one long, one short).
    """
    signals = []
    if len(day_indices) < 2:
        return signals

    c1_idx = None
    c2_idx = None
    zone_high = 0
    zone_low = 0
    c1_is_green = None

    # Step 1 & 2: Find C1 and C2 within today's market-hours candles
    for pos in range(1, len(day_indices)):
        i = day_indices[pos]
        i_prev = day_indices[pos - 1]

        vol_curr = df.at[i, "volume"]
        vol_prev = df.at[i_prev, "volume"]
        is_green = df.at[i, "is_green"]
        is_red = df.at[i, "is_red"]
        vol_spike = vol_curr > vol_prev

        if c1_idx is None:
            # C1: first volume spike that is either green or red
            if vol_spike and (is_green or is_red):
                c1_idx = i
                c1_is_green = is_green

        elif c2_idx is None:
            # C2: next opposite-color volume spike
            is_opposite = (c1_is_green and is_red) or (not c1_is_green and is_green)
            if vol_spike and is_opposite:
                c2_idx = i
                zone_high = max(df.at[c1_idx, "high"], df.at[c2_idx, "high"])
                zone_low = min(df.at[c1_idx, "low"], df.at[c2_idx, "low"])

                # Step 3: Detect breakouts in remaining candles after C2
                breakout_found = False
                breakdown_found = False

                for k_pos in range(pos + 1, len(day_indices)):
                    k = day_indices[k_pos]

                    close_k = df.at[k, "close"]
                    e1 = df.at[k, "e8"]
                    e2 = df.at[k, "e21"]
                    e3 = df.at[k, "e50"]
                    e4 = df.at[k, "e100"]
                    bv = df.at[k, "buy_v"]
                    sv = df.at[k, "sell_v"]

                    # Check EMAs are valid (warm-up)
                    if pd.isna(e4):
                        continue

                    # Long Breakout
                    if not breakout_found and close_k > zone_high:
                        # Full bullish stack + close > EMA(8) + delta
                        trend_ok = (e1 > e2 > e3 > e4) and (close_k > e1)
                        delta_ok = bv > sv
                        if trend_ok and delta_ok:
                            signals.append({
                                "type": "BUY",
                                "bar_idx": k,
                                "entry_px": close_k,
                                "sl": zone_low,
                                "zone_high": zone_high,
                                "zone_low": zone_low,
                                "c1_idx": c1_idx,
                                "c2_idx": c2_idx,
                            })
                            breakout_found = True

                    # Short Breakdown
                    if not breakdown_found and close_k < zone_low:
                        # Full bearish stack + close < EMA(8) + delta
                        trend_ok = (e1 < e2 < e3 < e4) and (close_k < e1)
                        delta_ok = sv > bv
                        if trend_ok and delta_ok:
                            signals.append({
                                "type": "SELL",
                                "bar_idx": k,
                                "entry_px": close_k,
                                "sl": zone_high,
                                "zone_high": zone_high,
                                "zone_low": zone_low,
                                "c1_idx": c1_idx,
                                "c2_idx": c2_idx,
                            })
                            breakdown_found = True

                    if breakout_found and breakdown_found:
                        break

                break  # Move to next symbol (one zone per day)

    return signals


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE OUTCOME SIMULATION
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_outcome(df, signal_idx, day_end_idx, entry_price, side,
                     sl_price, target_pct, stop_pct):
    """Walk remaining intraday bars to determine trade outcome."""
    if side == "BUY":
        target_px = entry_price * (1 + target_pct / 100)
        stop_px = max(sl_price, entry_price * (1 - stop_pct / 100))
    else:
        target_px = entry_price * (1 - target_pct / 100)
        stop_px = min(sl_price, entry_price * (1 + stop_pct / 100))

    for i in range(signal_idx + 1, day_end_idx + 1):
        h = df.at[i, "high"]
        l = df.at[i, "low"]

        if side == "BUY":
            if l <= stop_px:
                return {"exit": "STOP", "exit_px": round(stop_px, 2),
                        "pnl_pct": round((stop_px - entry_price) / entry_price * 100, 2)}
            if h >= target_px:
                return {"exit": "TARGET", "exit_px": round(target_px, 2),
                        "pnl_pct": round((target_px - entry_price) / entry_price * 100, 2)}
        else:
            if h >= stop_px:
                return {"exit": "STOP", "exit_px": round(stop_px, 2),
                        "pnl_pct": round((entry_price - stop_px) / entry_price * 100, 2)}
            if l <= target_px:
                return {"exit": "TARGET", "exit_px": round(target_px, 2),
                        "pnl_pct": round((entry_price - target_px) / entry_price * 100, 2)}

    close_px = df.at[day_end_idx, "close"]
    if side == "BUY":
        pnl = round((close_px - entry_price) / entry_price * 100, 2)
    else:
        pnl = round((entry_price - close_px) / entry_price * 100, 2)

    return {"exit": "EOD_CLOSE", "exit_px": round(close_px, 2), "pnl_pct": pnl}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST
# ═══════════════════════════════════════════════════════════════════════════════

def run_backtest(lookback_days=365, target_pct=DEFAULT_TARGET_PCT,
                 stop_pct=DEFAULT_STOP_PCT, out_csv="vcp_backtest_results.csv"):

    watchlist = load_watchlist()
    all_data = load_and_prepare(watchlist)

    if not all_data:
        sys.exit("❌ No data loaded.")

    # Build per-symbol, per-day market-hours indices
    print("🔧 Pre-computing daily market-hours boundaries …", flush=True)
    sym_day_market = {}  # symbol -> {date: [list of df indices in market hours]}
    for symbol, df in all_data.items():
        market_df = df[df["in_market"]]
        day_groups = market_df.groupby("date").apply(lambda g: g.index.tolist())
        sym_day_market[symbol] = dict(day_groups)

    # Determine trading dates
    all_dates = sorted({d for sym_days in sym_day_market.values() for d in sym_days})
    cutoff = date.today() - timedelta(days=lookback_days)
    trade_dates = [d for d in all_dates if d >= cutoff]

    if not trade_dates:
        sys.exit("❌ No trading dates in the selected window.")

    print(f"📅 Walk-forward: {len(trade_dates)} days ({trade_dates[0]} → {trade_dates[-1]})")
    print(f"🎯 Target: +{target_pct}% | 🛑 Stop: -{stop_pct}% | "
          f"Capital: ₹{TOTAL_CAPITAL:,} | Max trades/day: {MAX_TRADES_DAY}\n")

    records = []
    total_days = len(trade_dates)

    for di, scan_date in enumerate(trade_dates):
        if (di + 1) % 5 == 0 or di == 0 or di == total_days - 1:
            pct = (di + 1) / total_days * 100
            print(f"\r⏳ {di+1}/{total_days} ({pct:5.1f}%) [{scan_date}]", end="", flush=True)

        day_trades = 0

        for symbol, df in all_data.items():
            if day_trades >= MAX_TRADES_DAY:
                break

            day_map = sym_day_market.get(symbol, {})
            if scan_date not in day_map:
                continue

            day_indices = day_map[scan_date]
            if len(day_indices) < 2:
                continue

            # Scan this day for VCP signals (C1/C2/breakout all within today)
            signals = scan_symbol_day(df, day_indices)
            if not signals:
                continue

            # Take the first signal (live scanner fires one per direction)
            sig = signals[0]
            entry_price = sig["entry_px"]
            qty = int(BUYING_POWER / entry_price)
            if qty < 1:
                continue

            day_end_idx = day_indices[-1]

            outcome = simulate_outcome(
                df, sig["bar_idx"], day_end_idx,
                entry_price, sig["type"], sig["sl"],
                target_pct, stop_pct
            )

            trade_pnl = outcome["pnl_pct"]
            gross_pnl = round(entry_price * qty * trade_pnl / 100, 2)

            records.append({
                "date": scan_date,
                "symbol": symbol,
                "side": sig["type"],
                "entry_px": round(entry_price, 2),
                "sl_zone": round(sig["sl"], 2),
                "zone_high": round(sig["zone_high"], 2),
                "zone_low": round(sig["zone_low"], 2),
                "qty": qty,
                "exit_type": outcome["exit"],
                "exit_px": outcome["exit_px"],
                "pnl_pct": trade_pnl,
                "pnl_rs": gross_pnl,
            })
            day_trades += 1

    print()

    if not records:
        print("❌ No trades generated. Check data coverage.")
        return

    df_out = pd.DataFrame(records)
    df_out.to_csv(out_csv, index=False)
    print(f"\n💾 Saved {len(df_out):,} trades → {out_csv}\n")

    # ══════════════════════════════════════════════════════════════════════════
    # SUMMARY REPORT
    # ══════════════════════════════════════════════════════════════════════════
    _print_report(df_out, trade_dates, total_days, out_csv)


def _print_report(df_out, trade_dates, total_days, out_csv):
    print("=" * 72)
    print("📊  VCP BREAKOUT STRATEGY — BACKTEST SUMMARY")
    print("=" * 72)

    total = len(df_out)
    targets = (df_out["exit_type"] == "TARGET").sum()
    stops   = (df_out["exit_type"] == "STOP").sum()
    eods    = (df_out["exit_type"] == "EOD_CLOSE").sum()

    win_rate = targets / total * 100
    loss_rate = stops / total * 100

    buys  = df_out[df_out["side"] == "BUY"]
    sells = df_out[df_out["side"] == "SELL"]

    print(f"  Period               : {trade_dates[0]} → {trade_dates[-1]}")
    print(f"  Trading days         : {total_days}")
    print(f"  Total trades         : {total:,}")
    print(f"  Unique symbols       : {df_out['symbol'].nunique()}")
    print(f"  BUY signals          : {len(buys):,}")
    print(f"  SELL signals         : {len(sells):,}")
    print()

    print("── OUTCOMES ────────────────────────────────────────────────────────")
    print(f"  🎯 Target Hit        : {targets:5,d}  ({win_rate:.1f}%)")
    print(f"  🛑 Stop Hit          : {stops:5,d}  ({loss_rate:.1f}%)")
    print(f"  📉 EOD Close         : {eods:5,d}  ({eods/total*100:.1f}%)")
    print()

    avg_pnl = df_out["pnl_pct"].mean()
    med_pnl = df_out["pnl_pct"].median()
    total_pnl_rs = df_out["pnl_rs"].sum()
    max_win = df_out["pnl_pct"].max()
    max_loss = df_out["pnl_pct"].min()

    print("── RETURNS ─────────────────────────────────────────────────────────")
    print(f"  Avg trade P&L        : {avg_pnl:+.2f}%")
    print(f"  Median trade P&L     : {med_pnl:+.2f}%")
    print(f"  Max winning trade    : {max_win:+.2f}%")
    print(f"  Max losing trade     : {max_loss:+.2f}%")
    print(f"  Total P&L (₹)       : ₹{total_pnl_rs:+,.0f}")
    print(f"  ROI on capital       : {total_pnl_rs/TOTAL_CAPITAL*100:+.2f}%")
    print()

    # Expectancy
    winners = df_out[df_out["pnl_pct"] > 0]
    losers  = df_out[df_out["pnl_pct"] < 0]
    avg_win  = winners["pnl_pct"].mean() if len(winners) else 0
    avg_loss = losers["pnl_pct"].mean() if len(losers) else 0
    expectancy = (len(winners)/total * avg_win) + (len(losers)/total * avg_loss)
    print(f"  💰 Expectancy/trade  : {expectancy:+.3f}%")
    verdict = "✅ POSITIVE EDGE" if expectancy > 0 else "❌ NEGATIVE EDGE"
    print(f"  Verdict              : {verdict}")
    print()

    # Side split
    print("── SIDE SPLIT ──────────────────────────────────────────────────────")
    for side, emoji in [("BUY", "🟢"), ("SELL", "🔴")]:
        sub = df_out[df_out["side"] == side]
        if sub.empty:
            continue
        tw = (sub["exit_type"] == "TARGET").sum()
        print(f"  {emoji} {side:4s}: {len(sub):4,d} trades | "
              f"Win {tw/len(sub)*100:4.1f}% | "
              f"Avg {sub['pnl_pct'].mean():+.2f}% | "
              f"P&L ₹{sub['pnl_rs'].sum():+,.0f}")
    print()

    # Monthly
    df_out["month"] = pd.to_datetime(df_out["date"]).dt.to_period("M")
    monthly = (df_out.groupby("month")
               .agg(trades=("pnl_pct", "count"),
                    win_rate=("exit_type", lambda x: (x == "TARGET").mean() * 100),
                    avg_return=("pnl_pct", "mean"),
                    total_pnl=("pnl_rs", "sum"))
               .reset_index())

    print("── MONTHLY PERFORMANCE ─────────────────────────────────────────────")
    print(f"  {'Month':<10} {'Trades':>6} {'Win%':>7} {'Avg Ret':>9} {'P&L (₹)':>12}")
    print(f"  {'-'*10} {'-'*6} {'-'*7} {'-'*9} {'-'*12}")
    for _, row in monthly.iterrows():
        bar = "█" * int(max(0, row["avg_return"]) * 5)
        print(f"  {str(row['month']):<10} {int(row['trades']):>6} "
              f"{row['win_rate']:>6.1f}% {row['avg_return']:>+8.2f}% "
              f"₹{row['total_pnl']:>+10,.0f}  {bar}")
    print()

    # Risk metrics
    df_out["cum_pnl"] = df_out["pnl_rs"].cumsum()
    peak = df_out["cum_pnl"].cummax()
    drawdown = df_out["cum_pnl"] - peak
    max_dd = drawdown.min()
    max_dd_pct = max_dd / TOTAL_CAPITAL * 100

    print("── RISK METRICS ────────────────────────────────────────────────────")
    print(f"  Max drawdown (₹)    : ₹{max_dd:+,.0f}")
    print(f"  Max drawdown (%)    : {max_dd_pct:+.2f}%")

    gw = df_out[df_out["pnl_rs"] > 0]["pnl_rs"].sum()
    gl = abs(df_out[df_out["pnl_rs"] < 0]["pnl_rs"].sum())
    pf = gw / gl if gl > 0 else float("inf")
    print(f"  Profit factor        : {pf:.2f}")

    signs = (df_out["pnl_pct"] > 0).astype(int)
    streaks = signs.groupby((signs != signs.shift()).cumsum())
    w_streaks = [len(g) for _, g in streaks if g.iloc[0] == 1]
    l_streaks = [len(g) for _, g in streaks if g.iloc[0] == 0]
    print(f"  Max win streak       : {max(w_streaks) if w_streaks else 0}")
    print(f"  Max loss streak      : {max(l_streaks) if l_streaks else 0}")
    print()

    # Top/worst symbols
    sym_stats = (df_out.groupby("symbol")
                 .agg(trades=("pnl_pct", "count"),
                      avg_ret=("pnl_pct", "mean"),
                      total=("pnl_rs", "sum"),
                      wins=("exit_type", lambda x: (x == "TARGET").sum()))
                 .assign(win_pct=lambda x: x["wins"] / x["trades"] * 100)
                 .query("trades >= 3")
                 .sort_values("total", ascending=False))

    if not sym_stats.empty:
        print("── TOP 10 SYMBOLS (by total P&L, min 3 trades) ──────────────────")
        print(f"  {'Symbol':<14} {'Trades':>5} {'Win%':>7} {'Avg Ret':>9} {'Total P&L':>12}")
        print(f"  {'-'*14} {'-'*5} {'-'*7} {'-'*9} {'-'*12}")
        for sym, row in sym_stats.head(10).iterrows():
            print(f"  {sym:<14} {int(row['trades']):>5} "
                  f"{row['win_pct']:>6.1f}% {row['avg_ret']:>+8.2f}% "
                  f"₹{row['total']:>+10,.0f}")
        print()

        print("── WORST 5 SYMBOLS ─────────────────────────────────────────────────")
        print(f"  {'Symbol':<14} {'Trades':>5} {'Win%':>7} {'Avg Ret':>9} {'Total P&L':>12}")
        print(f"  {'-'*14} {'-'*5} {'-'*7} {'-'*9} {'-'*12}")
        for sym, row in sym_stats.tail(5).iterrows():
            print(f"  {sym:<14} {int(row['trades']):>5} "
                  f"{row['win_pct']:>6.1f}% {row['avg_ret']:>+8.2f}% "
                  f"₹{row['total']:>+10,.0f}")
        print()

    print("=" * 72)
    print(f"📁 Full trade log: {out_csv}")
    print("=" * 72)


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="VCP Breakout Strategy — Walk-Forward Backtest"
    )
    parser.add_argument("--days", type=int, default=365,
                        help="Lookback in calendar days (default: 365)")
    parser.add_argument("--target-pct", type=float, default=DEFAULT_TARGET_PCT,
                        help=f"Target profit %% (default: {DEFAULT_TARGET_PCT})")
    parser.add_argument("--sl-pct", type=float, default=DEFAULT_STOP_PCT,
                        help=f"Stop loss %% (default: {DEFAULT_STOP_PCT})")
    parser.add_argument("--csv", type=str, default="vcp_backtest_results.csv",
                        help="Output CSV path")
    args = parser.parse_args()

    run_backtest(
        lookback_days=args.days,
        target_pct=args.target_pct,
        stop_pct=args.sl_pct,
        out_csv=args.csv,
    )
