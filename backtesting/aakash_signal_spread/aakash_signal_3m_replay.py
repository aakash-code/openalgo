#!/usr/bin/env python3
"""
Faithful Python replay of "Intraday BUY/SELL & AUTO SL by Aakash" (Pine v6)
==========================================================================
Ports the indicator's signal engine bar-by-bar and replays it on NIFTY
3-minute bars for a single day (default 2026-06-12) so the generated
BUY/SELL signals can be validated against the TradingView chart BEFORE any
option legs are wired in.

Defaults mirror the Pine inputs:
  enhancedMode=True, session 0915-1525, enableBuy/Sell/Double=True,
  uniqueSignal=False, useTrendFilter=True (EMA 8/21/50/100 + VWAP),
  useAtrSl=False (SL = candle High/Low).

SIGNAL RULES (pure OHLC — fully faithful):
  buy1  : green now, red prev,            close > high[1]
  sell1 : red   now, green prev,          close < low[1]
  buy2  : green now, green prev, red  prev2, close > high[2]   (double)
  sell2 : red   now, red   prev, green prev2, close < low[2]   (double)

STATEFUL behaviour also ported: active-line break (= SL hit), forced
opposite signal on SL hit, slModeOnly latch, zone suppression, uniqueSignal.

CAVEATS (see chat):
  * NIFTY index volume is 0 in the DB -> true volume-VWAP is impossible.
    VWAP here = running mean of hlc3 since the session open (uniform-volume
    degenerate case). The TradingView chart may use real volume, so the
    VWAP *filter* can differ. RAW (pre-filter) signals are unaffected.
  * EMAs are warmed up over months of prior 3m bars (Pine EMAs are
    continuous across sessions; only VWAP resets daily).

Run: uv run python backtesting/nifty_options_selling/aakash_signal_3m_replay.py
Env: DAY=YYYY-MM-DD  USE_TREND_FILTER=1/0  USE_ATR_SL=1/0
"""
from __future__ import annotations

import os
from datetime import datetime, time as dtime
from pathlib import Path

import duckdb
import pandas as pd

# ---- Inputs (Pine defaults) -------------------------------------------------
DAY              = os.getenv("DAY", "2026-06-12")
ENHANCED_MODE    = True
ENABLE_BUY       = True
ENABLE_SELL      = True
ENABLE_DOUBLE    = True
UNIQUE_SIGNAL    = False
USE_TREND_FILTER = os.getenv("USE_TREND_FILTER", "1") == "1"
USE_ATR_SL       = os.getenv("USE_ATR_SL", "0") == "1"
ATR_LEN          = 14
ATR_MULT         = 1.5
EMA_LENS         = (8, 21, 50, 100)
TF_MIN           = int(os.getenv("TF_MIN", "3"))  # chart timeframe in minutes (1, 3, 5, ...)
SESS_START       = dtime(9, 15)
SESS_END         = dtime(15, 25)
WARMUP_DAYS      = 120  # calendar days of history to seed the EMAs

_script_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_script_dir / ".." / ".." / "db" / "historify.duckdb")


def load_3m(conn, day: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (full 3m series with indicators, just the `day` in-session slice)."""
    day_d = datetime.strptime(day, "%Y-%m-%d").date()
    start_ts = int((datetime.combine(day_d, dtime()) -
                    pd.Timedelta(days=WARMUP_DAYS)).timestamp())
    end_ts = int(datetime.combine(day_d, dtime(23, 59)).timestamp())
    df = conn.execute("""
        SELECT timestamp, open, high, low, close, volume
        FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp
    """, [start_ts, end_ts]).df()
    df["dt"] = (pd.to_datetime(df["timestamp"], unit="s", utc=True)
                  .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    df = df.set_index("dt").drop(columns=["timestamp"])
    df = df.between_time("09:15", "15:29")

    # 1m -> TF_MIN bars (left-closed/left-labelled; 09:15 aligns to the grid).
    bars = (df.resample(f"{TF_MIN}min", closed="left", label="left")
              .agg(open=("open", "first"), high=("high", "max"),
                   low=("low", "min"), close=("close", "last"),
                   volume=("volume", "sum"))
              .dropna())
    bars = bars.between_time("09:15", "15:25")  # session 0915-1525

    # Continuous EMAs (across sessions, like Pine ta.ema).
    for ln in EMA_LENS:
        bars[f"ema{ln}"] = bars["close"].ewm(span=ln, adjust=False).mean()

    # ATR (Wilder) continuous.
    prev_close = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"],
                    (bars["high"] - prev_close).abs(),
                    (bars["low"] - prev_close).abs()], axis=1).max(axis=1)
    bars["atr"] = tr.ewm(alpha=1.0 / ATR_LEN, adjust=False).mean()

    # Session VWAP proxy (volume=0 -> running mean of hlc3 from the open).
    day_slice = bars[bars.index.date == day_d].copy()
    hlc3 = (day_slice["high"] + day_slice["low"] + day_slice["close"]) / 3.0
    day_slice["vwap"] = hlc3.expanding().mean()
    return bars, day_slice


def replay(day_df: pd.DataFrame, use_trend_filter: bool = USE_TREND_FILTER,
           use_atr_sl: bool = USE_ATR_SL) -> list[dict]:
    """Bar-by-bar port of the indicator's signal state machine for one session."""
    o = day_df["open"].values
    h = day_df["high"].values
    lo = day_df["low"].values
    c = day_df["close"].values
    vwap = day_df["vwap"].values
    atr = day_df["atr"].values
    e = {ln: day_df[f"ema{ln}"].values for ln in EMA_LENS}
    idx = day_df.index
    n = len(day_df)

    lines: list[dict] = []   # {level, kind, active, born, entry}
    sl_mode_only = False
    last_signal = 0
    signals: list[dict] = []

    def green(i):  return c[i] > o[i]
    def red(i):    return c[i] < o[i]

    for i in range(n):
        new_session = (i == 0)            # only this session is loaded
        if new_session:
            lines.clear(); last_signal = 0; sl_mode_only = False

        prev1 = i >= 1
        prev2 = i >= 2
        prevGreen  = prev1 and c[i-1] > o[i-1]
        prevRed    = prev1 and c[i-1] < o[i-1]
        prev2Green = prev2 and c[i-2] > o[i-2]
        prev2Red   = prev2 and c[i-2] < o[i-2]

        buy1  = ENABLE_BUY  and green(i) and prevRed   and prev1 and (c[i] > h[i-1] if prev1 else False)
        sell1 = ENABLE_SELL and red(i)   and prevGreen and prev1 and (c[i] < lo[i-1] if prev1 else False)
        buy2  = ENABLE_DOUBLE and ENABLE_BUY  and green(i) and prevGreen and prev2Red   and prev2 and (c[i] > h[i-2])
        sell2 = ENABLE_DOUBLE and ENABLE_SELL and red(i)   and prevRed   and prev2Green and prev2 and (c[i] < lo[i-2])
        buyCondBase, sellCondBase = (buy1 or buy2), (sell1 or sell2)

        has_active_buy  = any(l["active"] and l["kind"] == 1  for l in lines)
        has_active_sell = any(l["active"] and l["kind"] == -1 for l in lines)

        buyCond  = buyCondBase  and (last_signal != 1  if UNIQUE_SIGNAL else not has_active_buy)  and not new_session
        sellCond = sellCondBase and (last_signal != -1 if UNIQUE_SIGNAL else not has_active_sell) and not new_session

        # Trend filters
        emaUp   = (e[8][i] > e[21][i]) or (e[21][i] > e[50][i]) or (e[50][i] > e[100][i])
        emaDown = (e[8][i] < e[21][i]) or (e[21][i] < e[50][i]) or (e[50][i] < e[100][i])
        buyFilters  = (not use_trend_filter) or (c[i] > vwap[i] and emaUp)
        sellFilters = (not use_trend_filter) or (c[i] < vwap[i] and emaDown)

        block_buy = block_sell = False
        forced_done = False

        # --- Active-line break loop (= SL hit) ---
        for l in lines:
            if not l["active"]:
                continue
            k, lvl = l["kind"], l["level"]
            brk = (k == 1 and c[i] < lvl and i > l["born"]) or \
                  (k == -1 and c[i] > lvl and i > l["born"])
            if not brk:
                continue
            l["active"] = False
            if ENHANCED_MODE and not forced_done:
                if k == 1 and sellFilters:        # broken BUY -> forced SELL
                    lvlNew = (c[i] + atr[i] * ATR_MULT) if use_atr_sl else h[i]
                    lines.append({"level": lvlNew, "kind": -1, "active": True,
                                  "born": i, "entry": c[i]})
                    last_signal = -1; block_sell = True; sl_mode_only = True
                    signals.append(_sig(idx[i], "SELL", "forced-on-SL", c[i], lvlNew, c[i]))
                elif k == -1 and buyFilters:      # broken SELL -> forced BUY
                    lvlNew = (c[i] - atr[i] * ATR_MULT) if use_atr_sl else lo[i]
                    lines.append({"level": lvlNew, "kind": 1, "active": True,
                                  "born": i, "entry": c[i]})
                    last_signal = 1; block_buy = True; sl_mode_only = True
                    signals.append(_sig(idx[i], "BUY", "forced-on-SL", c[i], lvlNew, c[i]))
                forced_done = True

        # Active levels AFTER break loop (for zone suppression)
        aBuyLvl = aBuyEntry = aSellLvl = aSellEntry = None
        for l in lines:
            if l["active"] and l["kind"] == 1:
                aBuyLvl, aBuyEntry = l["level"], l["entry"]
            if l["active"] and l["kind"] == -1:
                aSellLvl, aSellEntry = l["level"], l["entry"]

        insideBuyRedZone = (ENHANCED_MODE and aBuyLvl is not None and aBuyEntry is not None
                            and min(aBuyLvl, aBuyEntry) <= c[i] <= max(aBuyLvl, aBuyEntry))
        insideSellRedZone = (ENHANCED_MODE and aSellLvl is not None and aSellEntry is not None
                             and min(aSellEntry, aSellLvl) <= c[i] <= max(aSellEntry, aSellLvl))

        buyFire  = ((buyCond and not insideSellRedZone and not sl_mode_only) if ENHANCED_MODE
                    else buyCond) and buyFilters
        sellFire = ((sellCond and not insideBuyRedZone and not sl_mode_only) if ENHANCED_MODE
                    else sellCond) and sellFilters

        if buyFire and not block_buy:
            lvl = ((c[i] - atr[i] * ATR_MULT) if use_atr_sl
                   else (min(lo[i], lo[i-1]) if buy1 else min(lo[i], lo[i-1], lo[i-2])))
            lines.append({"level": lvl, "kind": 1, "active": True, "born": i, "entry": c[i]})
            last_signal = 1
            signals.append(_sig(idx[i], "BUY", "buy2(double)" if buy2 and not buy1 else "buy1",
                                c[i], lvl, c[i]))
        if sellFire and not block_sell:
            lvl = ((c[i] + atr[i] * ATR_MULT) if use_atr_sl
                   else (max(h[i], h[i-1]) if sell1 else max(h[i], h[i-1], h[i-2])))
            lines.append({"level": lvl, "kind": -1, "active": True, "born": i, "entry": c[i]})
            last_signal = -1
            signals.append(_sig(idx[i], "SELL", "sell2(double)" if sell2 and not sell1 else "sell1",
                                c[i], lvl, c[i]))

    return signals


def raw_patterns(day_df: pd.DataFrame) -> list[dict]:
    """Pure-OHLC pattern hits (no filters / no state) — the faithful ground truth."""
    o, h, lo, c = (day_df[k].values for k in ("open", "high", "low", "close"))
    idx = day_df.index
    out = []
    for i in range(len(day_df)):
        g = c[i] > o[i]; r = c[i] < o[i]
        pg = i >= 1 and c[i-1] > o[i-1]; pr = i >= 1 and c[i-1] < o[i-1]
        p2g = i >= 2 and c[i-2] > o[i-2]; p2r = i >= 2 and c[i-2] < o[i-2]
        if g and pr and i >= 1 and c[i] > h[i-1]:
            out.append(_sig(idx[i], "BUY", "buy1", c[i], min(lo[i], lo[i-1]), c[i]))
        elif g and pg and p2r and i >= 2 and c[i] > h[i-2]:
            out.append(_sig(idx[i], "BUY", "buy2(double)", c[i], min(lo[i], lo[i-1], lo[i-2]), c[i]))
        if r and pg and i >= 1 and c[i] < lo[i-1]:
            out.append(_sig(idx[i], "SELL", "sell1", c[i], max(h[i], h[i-1]), c[i]))
        elif r and pr and p2g and i >= 2 and c[i] < lo[i-2]:
            out.append(_sig(idx[i], "SELL", "sell2(double)", c[i], max(h[i], h[i-1], h[i-2]), c[i]))
    return out


def _sig(ts, side, rule, entry, sl, spot):
    return {"time": ts.strftime("%H:%M"), "side": side, "rule": rule,
            "entry": round(float(entry), 2), "sl": round(float(sl), 2),
            "spot": round(float(spot), 2)}


def _print(title, sigs):
    print(f"\n{title}  ({len(sigs)} signals)")
    print("-" * 64)
    if not sigs:
        print("  (none)")
        return
    print(f"  {'Time':<6}{'Side':<6}{'Rule':<16}{'Entry@close':>12}{'SL':>10}")
    for s in sigs:
        print(f"  {s['time']:<6}{s['side']:<6}{s['rule']:<16}{s['entry']:>12,.2f}{s['sl']:>10,.2f}")


def main():
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)
    bars, day_df = load_3m(conn, DAY)
    conn.close()

    print("=" * 64)
    print(f"AAKASH {TF_MIN}m SIGNAL REPLAY — {DAY}  (NIFTY spot)")
    print("=" * 64)
    print(f"  {TF_MIN}m bars in session : {len(day_df)}  "
          f"({day_df.index[0].time()}..{day_df.index[-1].time()})")
    print(f"  Trend filter       : {'ON (EMA8/21/50/100 + VWAP-proxy)' if USE_TREND_FILTER else 'OFF'}")
    print(f"  ATR SL             : {'ON' if USE_ATR_SL else 'OFF (candle High/Low)'}")
    print(f"  Day O/H/L/C        : {day_df['open'].iloc[0]:.2f} / {day_df['high'].max():.2f} "
          f"/ {day_df['low'].min():.2f} / {day_df['close'].iloc[-1]:.2f}")

    raw = raw_patterns(day_df)
    final = replay(day_df)

    _print("LAYER 1 — RAW pattern candidates (pure OHLC, 100% faithful)", raw)
    _print("LAYER 2 — FINAL signals (full state machine: filters + enhancedMode)", final)

    print("\nNote: Layer-1 is exact vs your chart's candles. Layer-2's VWAP uses a")
    print("uniform-volume proxy (index volume=0 in DB), so the trend filter may")
    print("differ slightly from TradingView. Compare Layer-1 first.")

    out = _script_dir / "results" / f"aakash_signals_{DAY}.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame(final).to_csv(out, index=False)
    print(f"\nFinal signals written: {out}")


if __name__ == "__main__":
    main()
