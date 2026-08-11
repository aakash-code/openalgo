#!/usr/bin/env python3
"""
CPR Breakout Equity Backtest
============================
Backtests the "narrow CPR + FRESH directional breakout" signal - the same
definition as the live IntradayBoost "CPR Breakout" filter (tf_cpr_service.py's
fixed-%% width threshold, bias from LTP vs TC/BC) restricted to the FIRST
crossing of the day only (matching the frontend's freshness fix - a stock
that gapped away from its zone hours ago and never came back is not traded
again that day).

Universe: the actual historical TradeFinder intraday_boost list, day by day,
from database/tf_boost_db.py (tf_boost_snapshots.duckdb) - the only period we
have point-in-time boost-list data for (2026-07-15 onward). This avoids
survivorship bias: each day only trades symbols that were REALLY on the boost
list that day, not today's list applied retroactively.

Entry: first 5m bar of the day where close breaks the (narrow) CPR zone.
Exit: 1% stop loss (direction-aware) OR EOD square-off (15:20 IST), whichever
first. Both directions traded (long on bullish break, short on bearish break).
Position size: Rs 50,000 margin x 5x leverage = Rs 2,50,000 notional/trade.

Run: uv run python backtesting/cpr_breakout_equity/cpr_breakout_backtest.py
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from database.auth_db import get_auth_token
from database.tf_boost_db import get_boost_symbols
from services.history_service import get_history

IST = ZoneInfo("Asia/Kolkata")
BROKER = "upstox"
AUTH_USER = "admin"

TRADING_DAYS = [
    "2026-07-15", "2026-07-16", "2026-07-17", "2026-07-21",
    "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-27",
]
CPR_WIDTH_THRESHOLD = 0.5  # % - matches IntradayBoost default "Narrow CPR" threshold
MARGIN_PER_TRADE = 50_000
LEVERAGE = 5
STOP_LOSS_PCT = 0.01
EOD_CUTOFF = datetime.strptime("15:20", "%H:%M").time()

# Equity intraday (MIS) cost model, consistent with this repo's cost_model convention:
# ~0.0225% combined exchange/regulatory charges + Rs 20/order brokerage + STT on the
# sell leg only (intraday equity STT is one-sided) + 18% GST on brokerage+txn.
BROKERAGE_PER_ORDER = 20
TXN_PCT = 0.0225 / 100
STT_INTRADAY_PCT = 0.025 / 100
GST_PCT = 0.18

_dir = Path(__file__).resolve().parent
RATE_LIMIT_SLEEP = 0.35  # keep the broker history API under its ~3 req/s cap


def fetch_daily(symbol: str, auth: str):
    success, data, _ = get_history(
        symbol=symbol, exchange="NSE", interval="D",
        start_date="2026-06-01", end_date="2026-07-27",
        auth_token=auth, broker=BROKER, source="api",
    )
    time.sleep(RATE_LIMIT_SLEEP)
    if not success:
        return None
    rows = data.get("data") or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST).dt.date
    return df.set_index("date")[["open", "high", "low", "close"]].sort_index()


def fetch_5m(symbol: str, auth: str):
    success, data, _ = get_history(
        symbol=symbol, exchange="NSE", interval="5m",
        start_date="2026-07-15", end_date="2026-07-27",
        auth_token=auth, broker=BROKER, source="api",
    )
    time.sleep(RATE_LIMIT_SLEEP)
    if not success:
        return None
    rows = data.get("data") or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["dt"] = (
        pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST).dt.tz_localize(None)
    )
    return df.set_index("dt").drop(columns=["timestamp"]).sort_index()


def cpr_for_day(daily_df: pd.DataFrame, day):
    prior_days = daily_df.index[daily_df.index < day]
    if len(prior_days) == 0:
        return None
    prev_day = prior_days[-1]
    h, l, c = daily_df.loc[prev_day, ["high", "low", "close"]]
    if not (h and l and c):
        return None
    cp = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * cp - bc
    top, bottom = max(tc, bc), min(tc, bc)
    if cp == 0:
        return None
    width_pct = (top - bottom) / cp * 100
    return width_pct, top, bottom


def leg_charges(entry: float, exit_: float, qty: int, side: str) -> float:
    turnover = (entry + exit_) * qty
    brokerage = BROKERAGE_PER_ORDER * 2  # entry + exit orders
    txn = TXN_PCT * turnover
    stt_base = exit_ if side == "long" else entry  # STT charged on the sell leg
    stt = STT_INTRADAY_PCT * stt_base * qty
    gst = GST_PCT * (brokerage + txn)
    return brokerage + txn + stt + gst


def run():
    universe_by_day = {}
    for day_str in TRADING_DAYS:
        d = datetime.strptime(day_str, "%Y-%m-%d").date()
        universe_by_day[d] = get_boost_symbols(day_str, day_str, "intraday_boost")

    all_symbols = sorted({s for syms in universe_by_day.values() for s in syms})
    print(f"Total unique symbols across {len(TRADING_DAYS)} days: {len(all_symbols)}")

    cache_dir = _dir / "results" / "raw_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    daily_cache: dict[str, pd.DataFrame] = {}
    intraday_cache: dict[str, pd.DataFrame] = {}

    auth = None
    for i, sym in enumerate(all_symbols):
        daily_path = cache_dir / f"{sym}_daily.parquet"
        intraday_path = cache_dir / f"{sym}_5m.parquet"
        if daily_path.exists() and intraday_path.exists():
            daily_cache[sym] = pd.read_parquet(daily_path)
            intraday_cache[sym] = pd.read_parquet(intraday_path)
        else:
            if auth is None:
                auth = get_auth_token(AUTH_USER)
            d_df = fetch_daily(sym, auth)
            i_df = fetch_5m(sym, auth)
            daily_cache[sym] = d_df
            intraday_cache[sym] = i_df
            if d_df is not None:
                d_df.to_parquet(daily_path)
            if i_df is not None:
                i_df.to_parquet(intraday_path)
        if (i + 1) % 25 == 0:
            print(f"  fetched/loaded {i + 1}/{len(all_symbols)} symbols...")

    trades = []
    for day_str in TRADING_DAYS:
        d = datetime.strptime(day_str, "%Y-%m-%d").date()
        for sym in universe_by_day[d]:
            daily_df = daily_cache.get(sym)
            intraday_df = intraday_cache.get(sym)
            if daily_df is None or daily_df.empty or intraday_df is None or intraday_df.empty:
                continue
            cpr = cpr_for_day(daily_df, d)
            if cpr is None:
                continue
            width_pct, top, bottom = cpr
            if width_pct > CPR_WIDTH_THRESHOLD:
                continue

            day_bars = intraday_df[intraday_df.index.date == d]
            if day_bars.empty:
                continue

            entry_ts = entry_price = side = None
            entry_idx = None
            for i, (ts, row) in enumerate(day_bars.iterrows()):
                if row["close"] > top:
                    entry_ts, side, entry_price, entry_idx = ts, "long", row["close"], i
                    break
                if row["close"] < bottom:
                    entry_ts, side, entry_price, entry_idx = ts, "short", row["close"], i
                    break
            if entry_ts is None:
                continue

            entry_volume = float(day_bars.iloc[entry_idx]["volume"])
            prior_bars = day_bars.iloc[:entry_idx]
            avg_prior_volume = float(prior_bars["volume"].mean()) if len(prior_bars) else float("nan")
            rel_volume = entry_volume / avg_prior_volume if avg_prior_volume else float("nan")
            breakout_strength_pct = (
                (entry_price - top) / top * 100 if side == "long" else (bottom - entry_price) / bottom * 100
            )

            qty = int((MARGIN_PER_TRADE * LEVERAGE) // entry_price)
            if qty <= 0:
                continue

            sl_price = (
                entry_price * (1 - STOP_LOSS_PCT) if side == "long" else entry_price * (1 + STOP_LOSS_PCT)
            )

            after = day_bars[day_bars.index > entry_ts]
            exit_price = exit_ts = exit_reason = None
            for ts, row in after.iterrows():
                if side == "long" and row["low"] <= sl_price:
                    exit_price, exit_ts, exit_reason = sl_price, ts, "sl"
                    break
                if side == "short" and row["high"] >= sl_price:
                    exit_price, exit_ts, exit_reason = sl_price, ts, "sl"
                    break
                if ts.time() >= EOD_CUTOFF:
                    exit_price, exit_ts, exit_reason = row["close"], ts, "eod"
                    break
            if exit_price is None:
                exit_price, exit_ts, exit_reason = after.iloc[-1]["close"], after.index[-1], "eod_lastbar"

            gross = (
                (exit_price - entry_price) * qty
                if side == "long"
                else (entry_price - exit_price) * qty
            )
            ch = leg_charges(entry_price, exit_price, qty, side)
            net = gross - ch

            trades.append({
                "date": d, "symbol": sym, "side": side,
                "entry_ts": entry_ts, "exit_ts": exit_ts,
                "entry": entry_price, "exit": exit_price, "qty": qty,
                "width_pct": width_pct, "top": top, "bottom": bottom,
                "breakout_strength_pct": breakout_strength_pct,
                "entry_volume": entry_volume, "avg_prior_volume": avg_prior_volume,
                "rel_volume": rel_volume,
                "exit_reason": exit_reason,
                "gross": gross, "charges": ch, "net": net,
            })

    df = pd.DataFrame(trades)
    out_dir = _dir / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "cpr_breakout_trades.csv", index=False)

    print("=" * 100)
    print(f"Trades: {len(df)}")
    if len(df):
        print(f"Net P&L: Rs {df['net'].sum():,.0f}  |  Gross: Rs {df['gross'].sum():,.0f}  |  "
              f"Charges: Rs {df['charges'].sum():,.0f}")
        print(f"Win rate: {(df['net'] > 0).mean() * 100:.1f}%")
        print("\nBy day:")
        print(df.groupby("date")["net"].agg(["count", "sum"]))
        print("\nBy exit reason:")
        print(df["exit_reason"].value_counts())
        print("\nBy side:")
        print(df.groupby("side")["net"].agg(["count", "sum", "mean"]))
    print(f"\nSaved: {out_dir / 'cpr_breakout_trades.csv'}")


if __name__ == "__main__":
    run()
