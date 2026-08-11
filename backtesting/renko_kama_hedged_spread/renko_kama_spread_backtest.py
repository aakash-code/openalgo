#!/usr/bin/env python3
"""
Renko + KAMA -> Delta-Targeted Hedged Credit Spreads (3-month grid backtest)
=============================================================================
Trend engine: NIFTY spot renko bricks + Kaufman's Adaptive Moving Average
(KAMA). Uptrend -> sell a PE credit spread (short delta ~0.45-0.55, long hedge
delta ~0.20-0.25). Downtrend -> the mirror CE credit spread. A spread stays
open until the trend flips (or end-of-day square-off); the very first defined
trend bar of a session re-enters if flat, so a multi-day trend keeps trading
without needing a fresh "flip" every morning.

Strikes are chosen by Black-Scholes delta (IV implied from each option's own
1m price), same pipeline as aakash_signal_spread/aakash_options_spread_test.py.

Since brick size and which series KAMA runs on are both open questions, this
script runs a GRID across:
  - brick size: several fixed point sizes, plus one ATR(14)-daily based size
  - KAMA variant A: KAMA computed on renko brick closes (trend = KAMA slope)
  - KAMA variant B: KAMA computed on raw 1m close (trend = price vs KAMA),
    sampled only at renko brick-close events to throttle whipsaw flips

...and reports P&L, trade count, win rate, max drawdown and a stability
metric (mean / std of weekly net P&L) per combo, so the most stable
configuration can be picked from real results rather than a guess.

Run: uv run python backtesting/renko_kama_hedged_spread/renko_kama_spread_backtest.py
Env: DAYS=90  LOTS=1  SHORT_LO=0.45 SHORT_HI=0.55  LONG_LO=0.20 LONG_HI=0.25  RATE=0.065
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from openalgo import ta

# ---- Config ------------------------------------------------------------
DAYS       = int(os.getenv("DAYS", "90"))
TIMEFRAME  = os.getenv("TIMEFRAME", "1m")  # '1m' or e.g. '5min' — resamples spot before Renko/KAMA/CHOP
LOTS       = int(os.getenv("LOTS", "1"))
SHORT_LO   = float(os.getenv("SHORT_LO", "0.45"))
SHORT_HI   = float(os.getenv("SHORT_HI", "0.55"))
LONG_LO    = float(os.getenv("LONG_LO", "0.20"))
LONG_HI    = float(os.getenv("LONG_HI", "0.25"))
RATE       = float(os.getenv("RATE", "0.065"))
STRIKE_STEP = 50
KAMA_LEN, KAMA_FAST, KAMA_SLOW = 14, 2, 30
SESSION_START, SESSION_END, SQUAREOFF = dtime(9, 15), dtime(15, 29), dtime(15, 24)

FIXED_BRICK_SIZES = [10, 15, 20, 25, 30]
ATR_MULTIPLIER = 0.5  # brick_size = prior day's daily ATR(14) * this

CHOP_PERIOD = 14
# None = no chop filter; a number = suppress entries/flips whenever the
# Choppiness Index (0-100, higher = more sideways/range-bound) is above it.
# 61.8 / 38.2 are the conventional Fibonacci-based chop/trend cutoffs.
CHOP_THRESHOLDS = [None, 61.8, 50.0, 38.2]

# EXIT_MODE="eod" (default, validated design, matches SQUAREOFF_TIME below):
# force-close every day at square-off, only flip signals or EOD end a trade.
# EXIT_MODE="carry": skip the daily square-off and let a position ride across
# days/weekends (captures more theta decay - see analyze_trades.py findings),
# still force-closing on/after the position's OWN contract expiry day so it
# never prices a settled/expired option.
EXIT_MODE = os.getenv("EXIT_MODE", "eod")
assert EXIT_MODE in ("eod", "carry"), f"EXIT_MODE must be 'eod' or 'carry', got {EXIT_MODE!r}"

BROKERAGE_PER_ORDER = 20
STT_SELL_PCT        = 0.001
TXN_CHARGE_PCT      = 0.0003553
SEBI_PER_CRORE      = 10
GST_PCT             = 0.18
STAMP_BUY_PCT       = 0.00003

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")


# ---- Black-Scholes (same model as aakash_options_spread_test.py) -------
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(opt: str, S: float, K: float, T: float, r: float, sig: float) -> float:
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if opt == "CE" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if opt == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(opt: str, price: float, S: float, K: float, T: float, r: float):
    intrinsic = max(0.0, (S - K) if opt == "CE" else (K - S))
    if price <= intrinsic + 1e-6 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(opt, S, K, T, r, hi) < price:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(opt, S, K, T, r, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_delta(opt: str, S: float, K: float, T: float, r: float, sig: float) -> float:
    if T <= 0 or sig <= 0:
        return (1.0 if S > K else 0.0) if opt == "CE" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return _ncdf(d1) if opt == "CE" else _ncdf(d1) - 1.0


def gst_of(brokerage, txn, sebi):
    return GST_PCT * (brokerage + txn + sebi)


def leg_charges(side: str, entry_px: float, exit_px: float, qty: int) -> float:
    brokerage = BROKERAGE_PER_ORDER * 2
    turnover = (entry_px + exit_px) * qty
    txn = TXN_CHARGE_PCT * turnover
    sebi = SEBI_PER_CRORE * turnover / 1e7
    if side == "short":
        stt, stamp = STT_SELL_PCT * entry_px * qty, STAMP_BUY_PCT * exit_px * qty
    else:
        stt, stamp = STT_SELL_PCT * exit_px * qty, STAMP_BUY_PCT * entry_px * qty
    return brokerage + stt + txn + sebi + gst_of(brokerage, txn, sebi) + stamp


# ---- Renko ---------------------------------------------------------------
def build_renko_bricks(close: pd.Series, brick_size: "float | pd.Series") -> pd.DataFrame:
    """Confirmed renko bricks from a 1m close series.

    brick_size: a constant float, or a pd.Series aligned to close.index giving
    a (possibly time-varying) brick size per bar.
    """
    is_dynamic = isinstance(brick_size, pd.Series)
    rows = []
    level = None
    for ts, price in close.items():
        bs = float(brick_size.loc[ts]) if is_dynamic else brick_size
        if bs <= 0 or np.isnan(bs):
            continue
        if level is None:
            level = round(price / bs) * bs
            continue
        while price >= level + bs:
            level += bs
            rows.append((ts, level, 1))
        while price <= level - bs:
            level -= bs
            rows.append((ts, level, -1))
    if not rows:
        return pd.DataFrame(columns=["close", "direction"])
    df = pd.DataFrame(rows, columns=["timestamp", "close", "direction"])
    return df.set_index("timestamp")


def atr_daily_brick_size(spot_1m: pd.DataFrame, multiplier: float) -> pd.Series:
    """Prior day's daily-ATR(14) * multiplier, broadcast (ffilled) across each
    trading day's minute bars — uses only information known before the day
    starts, so no lookahead."""
    daily = spot_1m.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    daily_atr = ta.atr(daily["high"].values, daily["low"].values, daily["close"].values, period=14)
    daily_atr = pd.Series(daily_atr, index=daily.index).shift(1) * multiplier  # prior day's ATR only
    per_bar = daily_atr.reindex(spot_1m.index.normalize()).ffill()
    per_bar.index = spot_1m.index
    return per_bar


# ---- KAMA trend variants ---------------------------------------------------
def trend_variant_a(close: pd.Series, brick_size) -> pd.Series:
    """KAMA on renko brick closes; trend = sign of KAMA slope between bricks."""
    bricks = build_renko_bricks(close, brick_size)
    if len(bricks) < KAMA_LEN + 2:
        return pd.Series(0, index=close.index)
    k = ta.kama(bricks["close"].values, length=KAMA_LEN, fast_length=KAMA_FAST, slow_length=KAMA_SLOW)
    slope = np.sign(np.diff(k, prepend=k[0]))
    at_brick = pd.Series(slope, index=bricks.index)
    return _stretch_to(at_brick, close.index)


def trend_variant_b(close: pd.Series, brick_size) -> pd.Series:
    """KAMA on raw 1m close (price vs KAMA), sampled at renko brick-close
    events only (throttles whipsaw flips to brick granularity)."""
    k = ta.kama(close.values, length=KAMA_LEN, fast_length=KAMA_FAST, slow_length=KAMA_SLOW)
    raw_sign = pd.Series(np.sign(close.values - k), index=close.index)
    bricks = build_renko_bricks(close, brick_size)
    if bricks.empty:
        return pd.Series(0, index=close.index)
    sampled = raw_sign.reindex(bricks.index, method="ffill")
    return _stretch_to(sampled, close.index)


def compute_chop(spot: pd.DataFrame, period: int = CHOP_PERIOD) -> pd.Series:
    """Choppiness Index on the spot 1m OHLC — causal (rolling window ending at
    each bar), 0-100, higher means more range-bound/sideways."""
    vals = ta.chop(spot["high"].values, spot["low"].values, spot["close"].values, period=period)
    return pd.Series(vals, index=spot.index)


def apply_chop_filter(trend: pd.Series, chop: pd.Series, threshold: "float | None") -> pd.Series:
    """Force the trend to neutral (0) wherever the market is judged too
    choppy to trust the signal. An already-open position is untouched by
    this (the day loop just holds through neutral bars) — only NEW entries
    and flips are suppressed."""
    if threshold is None:
        return trend
    masked = trend.copy()
    masked[chop > threshold] = 0
    return masked


def _stretch_to(step: pd.Series, full_index: pd.DatetimeIndex) -> pd.Series:
    """Forward-fill a sparse step series (indexed by event timestamps) across
    the full bar index; bars before the first event are neutral (0).

    A single fast-moving bar can confirm more than one renko brick, so `step`
    may carry duplicate timestamps — collapse to the last (final) value per
    timestamp before reindexing.
    """
    step = step[~step.index.duplicated(keep="last")]
    merged = step.reindex(step.index.union(full_index)).sort_index().ffill()
    return merged.reindex(full_index).fillna(0)


def resample_ohlc(spot_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 1m OHLC bars to a coarser timeframe (e.g. '5min').

    Bars are labeled on the RIGHT edge (bucket-close), not the left. A bucket
    covering [09:15, 09:20) has its close price known only at 09:20 — labeling
    it "09:15" (pandas' default) would let the backtest loop use that
    timestamp to fetch an option fill price from the *real* 09:15, while the
    signal was silently built from data up to 09:19: a lookahead bug. Labeling
    it "09:20" makes the bar's timestamp match the moment its data is actually
    known.
    """
    agg = spot_1m.resample(rule, label="right", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return agg.dropna()


# ---- Data loading -----------------------------------------------------------
def load_nifty_spot(conn, days: int) -> pd.DataFrame:
    max_ts = conn.execute(
        "SELECT MAX(timestamp) FROM market_data WHERE symbol='NIFTY' AND interval='1m'"
    ).fetchone()[0]
    min_ts = max_ts - days * 86400
    df = conn.execute(
        """
        SELECT timestamp, open, high, low, close FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m' AND timestamp >= ?
        ORDER BY timestamp
        """,
        [min_ts],
    ).df()
    df["dt"] = (
        pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    )
    df = df.set_index("dt").drop(columns=["timestamp"])
    return df.between_time(SESSION_START, SESSION_END)


def nearest_weekly_expiry(conn, day_d: date):
    row = conn.execute(
        """
        SELECT expiry_date, MIN(lot_size) lot
        FROM expired_fno_contracts
        WHERE openalgo_symbol LIKE 'NIFTY%' AND contract_type IN ('CE','PE')
          AND expiry_date >= ? GROUP BY expiry_date ORDER BY expiry_date LIMIT 1
        """,
        [day_d],
    ).fetchone()
    return (row[0], int(row[1])) if row else (None, None)


class OptionPriceCache:
    """Lazy per-symbol 1m close series, loaded once and reused across the
    whole grid (option data doesn't depend on trend config)."""

    def __init__(self, conn):
        self.conn = conn
        self.cache: dict[str, pd.DataFrame] = {}

    def _load(self, sym: str) -> pd.DataFrame:
        if sym not in self.cache:
            df = self.conn.execute(
                """
                SELECT timestamp, close FROM market_data
                WHERE symbol=? AND exchange='NFO' AND interval='1m' ORDER BY timestamp
                """,
                [sym],
            ).df()
            if not df.empty:
                df["dt"] = (
                    pd.to_datetime(df["timestamp"], unit="s", utc=True)
                    .dt.tz_convert("Asia/Kolkata")
                    .dt.tz_localize(None)
                )
                df = df.set_index("dt").drop(columns=["timestamp"])
            self.cache[sym] = df
        return self.cache[sym]

    def price(self, expiry: date, strike: int, ot: str, ts: pd.Timestamp):
        sym = f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(strike)}{ot}"
        df = self._load(sym)
        if df.empty:
            return None
        i = df.index.searchsorted(ts, side="right") - 1
        return float(df["close"].iloc[i]) if i >= 0 else None


def tte_years(ts: pd.Timestamp, expiry: date) -> float:
    secs = (datetime.combine(expiry, dtime(15, 30)) - ts.to_pydatetime()).total_seconds()
    return max(secs, 60) / (365.0 * 24 * 3600)


def pick_in_band(opt_cache, spot, ts, expiry, ot, lo, hi):
    """Scan strikes; prefer the one landing inside [lo, hi] |delta|, else the
    closest to the band center."""
    T = tte_years(ts, expiry)
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    center = (lo + hi) / 2
    candidates = []
    for off in range(-12, 13):
        K = atm + off * STRIKE_STEP
        px = opt_cache.price(expiry, K, ot, ts)
        if px is None or px <= 0.05:
            continue
        iv = implied_vol(ot, px, spot, K, T, RATE)
        if iv is None:
            continue
        d = abs(bs_delta(ot, spot, K, T, RATE, iv))
        candidates.append((K, px, d))
    if not candidates:
        return None
    in_band = [c for c in candidates if lo <= c[2] <= hi]
    pool = in_band if in_band else candidates
    return min(pool, key=lambda c: abs(c[2] - center))


# ---- Backtest engine (variant-agnostic; consumes a precomputed trend step) -
def run_backtest(spot: pd.DataFrame, trend: pd.Series, conn, opt_cache: OptionPriceCache):
    trades = []
    open_pos = None  # dict: side(PE/CE), sK,sEntry, lK,lEntry, qty, entry_ts, expiry
    prev_side = None

    for day, day_idx in spot.groupby(spot.index.date).groups.items():
        day_bars = spot.loc[day_idx]
        expiry, lot = nearest_weekly_expiry(conn, day)
        if expiry is None:
            continue
        qty = lot * LOTS

        for ts in day_bars.index:
            cur = trend.loc[ts]
            side = "PE" if cur > 0 else ("CE" if cur < 0 else None)
            spot_px = float(day_bars.at[ts, "close"])
            can_enter = ts.time() < SQUAREOFF

            # NOTE: side can legitimately be None (neutral/chop). The EOD
            # square-off check below must still run on every bar regardless -
            # it previously sat behind a `continue` on side is None, which let
            # a position silently carry into the next trading day whenever
            # the trend went neutral right at the close.
            if side is not None:
                if open_pos is None and can_enter:
                    short = pick_in_band(opt_cache, spot_px, ts, expiry, side, SHORT_LO, SHORT_HI)
                    long_ = pick_in_band(opt_cache, spot_px, ts, expiry, side, LONG_LO, LONG_HI)
                    if short and long_ and short[0] != long_[0]:
                        open_pos = {
                            "side": side, "entry_ts": ts, "expiry": expiry, "qty": qty,
                            "sK": short[0], "sEntry": short[1], "sDelta": short[2],
                            "lK": long_[0], "lEntry": long_[1], "lDelta": long_[2],
                        }
                        prev_side = side
                elif open_pos is not None and side != prev_side and can_enter:
                    _close_trade(trades, open_pos, opt_cache, ts, "flip")
                    short = pick_in_band(opt_cache, spot_px, ts, expiry, side, SHORT_LO, SHORT_HI)
                    long_ = pick_in_band(opt_cache, spot_px, ts, expiry, side, LONG_LO, LONG_HI)
                    open_pos = None
                    if short and long_ and short[0] != long_[0]:
                        open_pos = {
                            "side": side, "entry_ts": ts, "expiry": expiry, "qty": qty,
                            "sK": short[0], "sEntry": short[1], "sDelta": short[2],
                            "lK": long_[0], "lEntry": long_[1], "lDelta": long_[2],
                        }
                    prev_side = side

            must_close_eod = ts.time() >= SQUAREOFF and (
                EXIT_MODE == "eod" or (open_pos is not None and day >= open_pos["expiry"])
            )
            if must_close_eod and open_pos is not None:
                _close_trade(trades, open_pos, opt_cache, ts, "eod")
                open_pos = None
                prev_side = None

    if open_pos is not None:
        _close_trade(trades, open_pos, opt_cache, spot.index[-1], "eod")

    return pd.DataFrame(trades)


def _close_trade(trades: list, pos: dict, opt_cache: OptionPriceCache, exit_ts: pd.Timestamp, reason: str):
    ot = pos["side"]
    sExit = opt_cache.price(pos["expiry"], pos["sK"], ot, exit_ts) or pos["sEntry"]
    lExit = opt_cache.price(pos["expiry"], pos["lK"], ot, exit_ts) or pos["lEntry"]
    qty = pos["qty"]
    gross = (pos["sEntry"] - sExit) * qty + (lExit - pos["lEntry"]) * qty
    ch = leg_charges("short", pos["sEntry"], sExit, qty) + leg_charges("long", pos["lEntry"], lExit, qty)
    credit = pos["sEntry"] - pos["lEntry"]
    width = abs(pos["sK"] - pos["lK"])
    # Defined-risk spread: max loss = (strike width - net credit) x qty, which
    # is what a broker's SPAN+exposure margin for a hedged spread approximates
    # (the long leg caps the short leg's otherwise-uncapped naked exposure).
    margin = max(0.0, width - credit) * qty
    trades.append({
        "entry_ts": pos["entry_ts"], "exit_ts": exit_ts, "reason": reason,
        "struct": "BullPut" if ot == "PE" else "BearCall",
        "sell_K": pos["sK"], "sell_entry": pos["sEntry"], "sell_delta": round(pos["sDelta"], 2),
        "buy_K": pos["lK"], "buy_entry": pos["lEntry"], "buy_delta": round(pos["lDelta"], 2),
        "qty": qty, "credit": credit, "width": width, "margin": margin,
        "gross": gross, "charges": ch, "net": gross - ch,
    })


# ---- Metrics ----------------------------------------------------------------
def summarize(trades: pd.DataFrame, label: str) -> dict:
    if trades.empty:
        return {"config": label, "trades": 0, "net": 0.0, "win_rate": 0.0, "max_dd": 0.0,
                "stability": 0.0, "peak_margin": 0.0, "return_on_margin": 0.0}
    equity = trades["net"].cumsum()
    running_max = equity.cummax()
    max_dd = float((equity - running_max).min())
    weekly = trades.set_index("exit_ts")["net"].resample("W").sum()
    stability = float(weekly.mean() / weekly.std()) if weekly.std() not in (0, None) and len(weekly) > 1 else 0.0
    # Spreads are opened one at a time (never overlapping), so the capital
    # actually at risk at any moment is just that trade's own margin — peak
    # margin across the run is the largest single-trade figure, not a sum.
    peak_margin = float(trades["margin"].max())
    net = float(trades["net"].sum())
    return {
        "config": label,
        "trades": len(trades),
        "net": net,
        "win_rate": float((trades["net"] > 0).mean()),
        "max_dd": max_dd,
        "stability": stability,
        "peak_margin": peak_margin,
        "return_on_margin": net / peak_margin if peak_margin > 0 else 0.0,
    }


# ---- Main --------------------------------------------------------------------
def main():
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)
    print(f"Loading NIFTY spot 1m ({DAYS}d window) + building option price cache on demand...")
    spot = load_nifty_spot(conn, DAYS)
    if TIMEFRAME != "1m":
        spot = resample_ohlc(spot, TIMEFRAME)
    print(f"Timeframe: {TIMEFRAME}  Spot bars: {len(spot)}  span: {spot.index[0]} -> {spot.index[-1]}")
    opt_cache = OptionPriceCache(conn)

    brick_configs: list[tuple[str, "float | pd.Series"]] = [(f"fixed{n}", float(n)) for n in FIXED_BRICK_SIZES]
    brick_configs.append((f"atr{ATR_MULTIPLIER}", atr_daily_brick_size(spot, ATR_MULTIPLIER)))
    chop = compute_chop(spot)

    results = []
    all_trades = {}
    # Variant A (KAMA on renko closes) consistently beat variant B in the
    # unfiltered grid, so the chop-threshold sweep focuses there.
    for brick_label, brick_size in brick_configs:
        for chop_thr in CHOP_THRESHOLDS:
            thr_label = "off" if chop_thr is None else str(chop_thr)
            label = f"A_renkoKAMA_{TIMEFRAME}_{brick_label}_chop{thr_label}"
            raw_trend = trend_variant_a(spot["close"], brick_size)
            trend = apply_chop_filter(raw_trend, chop, chop_thr)
            trades = run_backtest(spot, trend, conn, opt_cache)
            summary = summarize(trades, label)
            results.append(summary)
            all_trades[label] = trades
            print(f"  {label:<32} trades={summary['trades']:>4}  net=Rs{summary['net']:>10,.0f}  "
                  f"win%={summary['win_rate']*100:>5.1f}  maxDD=Rs{summary['max_dd']:>10,.0f}  "
                  f"peakMargin=Rs{summary['peak_margin']:>9,.0f}  RoM={summary['return_on_margin']*100:>6.1f}%  "
                  f"stability={summary['stability']:>6.2f}")

    conn.close()

    res_df = pd.DataFrame(results).sort_values("stability", ascending=False)
    print("\n" + "=" * 100)
    print(f"RANKED BY STABILITY (mean weekly net P&L / std weekly net P&L)  [{DAYS}d, {LOTS} lot]")
    print("=" * 100)
    print(res_df.to_string(index=False))

    out_dir = _dir / "results"
    out_dir.mkdir(exist_ok=True)
    res_df.to_csv(out_dir / f"grid_summary_{TIMEFRAME}.csv", index=False)
    best = res_df.iloc[0]["config"]
    all_trades[best].to_csv(out_dir / f"trades_{best}.csv", index=False)
    print(f"\nGrid summary: {out_dir / f'grid_summary_{TIMEFRAME}.csv'}")
    print(f"Best-by-stability config trades: {out_dir / f'trades_{best}.csv'}")


if __name__ == "__main__":
    main()
