#!/usr/bin/env python3
"""
================================================================================
  BREAKOUT INTRADAY STRATEGY  —  OpenAlgo + TradeFinder
================================================================================
  Single-file live trading strategy. No webhook, no TradingView.

  Pine Script port: "Intraday Signals & Risk Calculator [India v5.6]"
  ------------------------------------------------------------------------------
  Watchlist  : TradeFinder IntradayBoost  (refreshed every WATCHLIST_REFRESH_SEC)
  Signal     : single- and double-candle breakout (Pine buy1/buy2/sell1/sell2)
  Filters    : VWAP (buy>VWAP, sell<VWAP)  +  ADX >= threshold   [ON by default]
  Sizing     : risk-based  qty = (capital x risk%) / stop_distance, notional-capped
  Entry      : MARKET on the breakout bar's close
  Stop       : broker-side SL-M at breakout candle low/high  (survives a crash)
  Target     : single target at TARGET_RR x risk  (0 = disabled, hold till EXIT_TIME)
  Breakeven  : move SL to entry once price moves BREAKEVEN_PCT% in your favour (0=off)
  Trail      : move SL to prior candle low/high once the trade is in profit
  Re-entry   : after a position closes, take the next signal on the same stock (ALLOW_REENTRY)
  Square-off : flatten everything at EXIT_TIME HHMM (before broker MIS SOS)

  Self-hosted /python ready: reads env vars, traps SIGTERM, logs to stdout.

  Usage (standalone):
      python breakout_intraday_strategy.py
      python breakout_intraday_strategy.py --test     # one diagnostic pass, no orders

  Usage (OpenAlgo /python runner):
      Upload, set parameters (SYMBOLS source = TradeFinder), schedule 09:15-15:30.
      Env vars OPENALGO_API_KEY / HOST_SERVER / WEBSOCKET_URL are read automatically.
================================================================================
"""

import argparse
import base64
import hashlib
import hmac
import json as _json
import logging
import math
import os
import signal
import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except Exception:
    pass

from openalgo import api as openalgo_api

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG  (every value is env-overridable so the /python upload form can tune it)
# ══════════════════════════════════════════════════════════════════════════════

# ── OpenAlgo connection (canonical env priority: HOST_SERVER > OPENALGO_HOST) ──
OPENALGO_API_KEY = os.getenv("OPENALGO_API_KEY", "")
OPENALGO_HOST    = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
OPENALGO_WS_URL  = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}")
EXCHANGE         = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))

STRATEGY_NAME    = os.getenv("STRATEGY_NAME", "BREAKOUT_INTRADAY")
PRODUCT          = os.getenv("PRODUCT", "MIS")          # intraday
INTERVAL         = os.getenv("INTERVAL", "5m")          # signal timeframe

# ── TradeFinder watchlist source ──────────────────────────────────────────────
# JWT is read DYNAMICALLY on every fetch — update without restarting the strategy.
# Priority: TF_JWT_FILE (file path) > TF_JWT_TOKEN (env var)
#
# Easiest morning workflow:
#   echo "<paste lt token>" > strategies/tf_jwt.txt
# Or set env: TF_JWT_TOKEN="eyJ..."
#
# To get the token: tradefinder.in → DevTools → Application → Local Storage → "lt"
TF_JWT_TOKEN          = os.getenv("TF_JWT_TOKEN", "")           # static fallback (read at startup)
TF_JWT_FILE           = os.getenv("TF_JWT_FILE",  os.path.join(os.path.dirname(__file__), "tf_jwt.txt"))
WATCHLIST_REFRESH_SEC = int(os.getenv("WATCHLIST_REFRESH_SEC", "60"))   # 1 minute
MAX_WATCHLIST         = int(os.getenv("MAX_WATCHLIST", "0"))             # 0 = all; else top-N by score


def _get_tf_jwt() -> str:
    """Read JWT dynamically: file first (hot-reloadable), then env var."""
    if TF_JWT_FILE and os.path.exists(TF_JWT_FILE):
        try:
            token = open(TF_JWT_FILE).read().strip()
            if token:
                return token
        except Exception:
            pass
    return TF_JWT_TOKEN

# ── Signal config (Pine defaults) ─────────────────────────────────────────────
ENABLE_BUY     = os.getenv("ENABLE_BUY",  "true").lower() == "true"
ENABLE_SELL    = os.getenv("ENABLE_SELL", "true").lower() == "true"
ENABLE_SINGLE  = os.getenv("ENABLE_SINGLE", "false").lower() == "true"  # single-candle breakouts (sweep: double-only wins)
ENABLE_DOUBLE  = os.getenv("ENABLE_DOUBLE", "true").lower() == "true"   # double-candle breakouts
USE_VWAP       = os.getenv("USE_VWAP", "true").lower() == "true"        # ON  (your choice)
USE_ADX        = os.getenv("USE_ADX",  "true").lower() == "true"        # ON  (your choice)
ADX_LEN        = int(os.getenv("ADX_LEN", "14"))
ADX_THRESHOLD  = float(os.getenv("ADX_THRESHOLD", "25"))               # sweep: 25 filters weak-trend fakeouts

# ── Risk / sizing (Pine "Risk Calculator" defaults) ───────────────────────────
BALANCE          = float(os.getenv("BALANCE", "311000"))      # trading capital
MAX_LOSS_PCT     = float(os.getenv("MAX_LOSS_PCT", "1.0"))    # % of capital risked per trade
CAPITAL_PER_TRADE  = float(os.getenv("CAPITAL_PER_TRADE", "50000"))  # notional cap per stock
MIN_SL_DIST_PCT    = float(os.getenv("MIN_SL_DIST_PCT", "0.3"))      # skip if SL < X% away (charges > profit)
TARGET_RR          = float(os.getenv("TARGET_RR", "2.0"))   # 0 = no fixed target (hold till EXIT_TIME)
# Trail mode: "none" | "after_1R" | "full"
# after_1R  — trail only after +1R profit (sweep winner, protects from noise)
# full      — trail every bar in profit (scratches winners on volatile days)
# none      — no trailing at all (just SL + target/time)
TRAIL_MODE       = os.getenv("TRAIL_MODE", "after_1R").lower()
# Breakeven: move SL to entry once price moves X% in your favour. 0 = disabled.
# Example: BREAKEVEN_PCT=1.0 → move SL to entry after 1% favourable move.
# Works with TRAIL_MODE — after breakeven activates, trail continues from entry.
BREAKEVEN_PCT    = float(os.getenv("BREAKEVEN_PCT", "0.0"))

# ── Session / risk guardrails ─────────────────────────────────────────────────
SESSION_START_HM = os.getenv("SESSION_START",  "0915")  # HHMM — session open
ENTRY_CUTOFF_HM  = os.getenv("ENTRY_CUTOFF",   "1500")  # HHMM — no NEW entries after this
EXIT_TIME_HM     = os.getenv("EXIT_TIME",       "1525")  # HHMM — voluntary square-off
HARD_EXIT_TIME_HM= os.getenv("HARD_EXIT_TIME",  "1520")  # HHMM — failsafe (< broker MIS SOS)
# Parse exit times into hours/minutes for backward compat
_et  = EXIT_TIME_HM.zfill(4);      EXIT_HOUR,      EXIT_MINUTE      = int(_et[:2]), int(_et[2:])
_het = HARD_EXIT_TIME_HM.zfill(4); HARD_EXIT_HOUR, HARD_EXIT_MINUTE = int(_het[:2]), int(_het[2:])

ALLOW_REENTRY    = os.getenv("ALLOW_REENTRY", "true").lower() == "true"  # re-enter after exit
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
MAX_DAILY_LOSS_RS  = float(os.getenv("MAX_DAILY_LOSS_RS", "25000"))
ORDER_DELAY_S      = float(os.getenv("ORDER_DELAY_S", "0.112"))   # ~9 orders/sec (< SEBI 10 OPS)
POLL_INTERVAL_SEC  = int(os.getenv("POLL_INTERVAL_SEC", "20"))    # main loop cadence

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"  # True = log only, no orders sent

# ── Log directory ─────────────────────────────────────────────────────────────
# Uses BREAKOUT_LOG_DIR (not LOG_DIR) to avoid conflict with OpenAlgo's own LOG_DIR env var.
BREAKOUT_LOG_DIR = os.getenv("BREAKOUT_LOG_DIR", "logs/breakout")

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING  (stdout + daily rotating files)
# ══════════════════════════════════════════════════════════════════════════════
_RUN_DATE   = datetime.now().strftime("%Y-%m-%d")
_LOG_DIR    = BREAKOUT_LOG_DIR
os.makedirs(_LOG_DIR, exist_ok=True)

_FMT = "%(asctime)s  %(levelname)-7s  %(message)s"

# Root handler: stdout (captured by /python host)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=_FMT, stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# File handler 1: full daily log  (INFO+)
_fh_all = logging.FileHandler(os.path.join(_LOG_DIR, f"breakout_{_RUN_DATE}.log"), encoding="utf-8")
_fh_all.setFormatter(logging.Formatter(_FMT))
_fh_all.setLevel(logging.INFO)
log.addHandler(_fh_all)

# File handler 2: errors-only log  (WARNING+) — quick post-day review
_fh_err = logging.FileHandler(os.path.join(_LOG_DIR, f"breakout_errors_{_RUN_DATE}.log"), encoding="utf-8")
_fh_err.setFormatter(logging.Formatter(_FMT))
_fh_err.setLevel(logging.WARNING)
log.addHandler(_fh_err)

# Paths
_LOG_JSON   = os.getenv("JSONL_PATH",  os.path.join(_LOG_DIR, f"breakout_events_{_RUN_DATE}.jsonl"))
_STATE_FILE = os.getenv("STATE_PATH",  os.path.join(_LOG_DIR, f"breakout_state_{_RUN_DATE}.json"))
_TRADE_CSV  = os.path.join(_LOG_DIR, f"breakout_trades_{_RUN_DATE}.csv")

import csv as _csv

# ── Trade CSV writer ──────────────────────────────────────────────────────────
_CSV_HEADER = [
    "date", "signal_time", "entry_time", "exit_time",
    "symbol", "direction", "kind",
    "signal_adx", "signal_vwap",
    "entry_price", "initial_sl", "final_sl", "target",
    "exit_price", "qty", "notional",
    "gross", "charges", "net", "reason",
    "breakeven_activated", "reached_1r",
    "dry_run",
]

def _write_trade_csv(pos, signal_time: str = "", signal_adx: float = 0.0,
                     signal_vwap: float = 0.0, kind: str = ""):
    """Append one completed trade row to the daily CSV."""
    if pos.exit_price == 0.0:
        return
    now_str = _now_ist().strftime("%H:%M:%S") if "_now_ist" in globals() else ""
    gross   = (pos.exit_price - pos.entry_price) * pos.qty * (1 if pos.direction == "LONG" else -1)
    ch      = compute_charges(pos.entry_price, pos.exit_price, pos.qty)
    net     = gross - ch
    notional = pos.entry_price * pos.qty

    write_header = not os.path.exists(_TRADE_CSV)
    try:
        with open(_TRADE_CSV, "a", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=_CSV_HEADER)
            if write_header:
                w.writeheader()
            w.writerow({
                "date":                 _RUN_DATE,
                "signal_time":          signal_time or getattr(pos, "signal_time", ""),
                "entry_time":           getattr(pos, "entry_time", ""),
                "exit_time":            now_str,
                "symbol":               pos.symbol,
                "direction":            pos.direction,
                "kind":                 kind or getattr(pos, "kind", ""),
                "signal_adx":           round(signal_adx or getattr(pos, "signal_adx", 0.0), 2),
                "signal_vwap":          round(signal_vwap or getattr(pos, "signal_vwap", 0.0), 2),
                "entry_price":          pos.entry_price,
                "initial_sl":           pos.initial_sl,
                "final_sl":             pos.current_sl,
                "target":               pos.target,
                "exit_price":           pos.exit_price,
                "qty":                  pos.qty,
                "notional":             round(notional, 2),
                "gross":                round(gross, 2),
                "charges":              round(ch, 2),
                "net":                  round(net, 2),
                "reason":               pos.exit_reason,
                "breakeven_activated":  pos.reached_be,
                "reached_1r":           pos.reached_1r,
                "dry_run":              DRY_RUN,
            })
    except Exception as e:
        log.warning(f"_write_trade_csv failed: {e}")


def _jlog(event: str, **data):
    """Append one structured JSON line for offline replay of trade-critical events."""
    try:
        with open(_LOG_JSON, "a") as f:
            f.write(_json.dumps({"ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                                 "event": event, **data}) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# SIGTERM-safe shutdown  (required for /python host)
# ══════════════════════════════════════════════════════════════════════════════
stop_event = threading.Event()


def _shutdown(signum, frame):
    log.info("Signal %d received — shutting down gracefully", signum)
    stop_event.set()


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


# ══════════════════════════════════════════════════════════════════════════════
# TRADEFINDER CLIENT  (lifted verbatim from the ANN strategy)
# ══════════════════════════════════════════════════════════════════════════════
_TF_BASE   = "https://tradefinder.in/api_be"
_TF_SECRET = "5ACHPKZUZNTWYPSJXNP7IULMACAM6P6Q"   # TOTP secret — do not change

import urllib3  # noqa: E402
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_TF_SSL_VERIFY = False   # LibreSSL workaround (macOS system Python). Use Py3.12 to remove.


def _generate_totp(ts_ms: int) -> str:
    """RFC 6238 TOTP — HMAC-SHA1, 30s window, 6 digits."""
    raw = _TF_SECRET.upper()
    key = base64.b32decode(raw + "=" * ((8 - len(raw) % 8) % 8))
    counter = int(ts_ms / 1000 / 30)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def _tf_server_time_ms() -> int:
    try:
        r = requests.get(f"{_TF_BASE}/servertime", timeout=5, verify=_TF_SSL_VERIFY)
        return int(r.json()["payload"]["data"])
    except Exception:
        return int(time.time() * 1000)


def fetch_intraday_boost() -> list[dict]:
    """Fetch TradeFinder IntradayBoost stocks. Returns list sorted by score desc."""
    jwt = _get_tf_jwt()
    if not jwt:
        log.error("TF_JWT_TOKEN is empty. Quick fix: echo '<token>' > strategies/tf_jwt.txt")
        return []
    totp = _generate_totp(_tf_server_time_ms())
    headers = {"jwttoken": jwt, "accesstoken": totp}
    try:
        data = requests.get(f"{_TF_BASE}/data/market_pulse", headers=headers,
                            timeout=10, verify=_TF_SSL_VERIFY).json()
    except Exception as e:
        log.error(f"TradeFinder request failed: {e}")
        return []
    if data.get("status") != "SUCCESS":
        log.error(f"TradeFinder error: {data.get('code')} — {data.get('message')} "
                  f"(JWT expired? paste a fresh token)")
        return []
    raw = (data.get("payload") or {}).get("data") or {}
    items_raw = raw.get("intraday_boost") or raw.get("intradayBoost") or []
    items = [{"symbol": r["Symbol"], "ltp": float(r["param_0"]),
              "prev_close": float(r["param_1"]), "change_pct": float(r["param_2"]),
              "score": float(r["param_3"])} for r in items_raw]
    items.sort(key=lambda x: x["score"], reverse=True)
    return items


# ══════════════════════════════════════════════════════════════════════════════
# OPENALGO CLIENT + DATA
# ══════════════════════════════════════════════════════════════════════════════
_client: Optional[openalgo_api] = None


def _get_client() -> openalgo_api:
    global _client
    if _client is None:
        _client = openalgo_api(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST, ws_url=OPENALGO_WS_URL)
    return _client


def fetch_history(symbol: str, interval: str, days: int) -> Optional[pd.DataFrame]:
    end = date.today()
    start = end - timedelta(days=days)
    api_interval = {"1d": "D", "1w": "W", "1M": "M"}.get(interval, interval)
    try:
        df = _get_client().history(symbol=symbol, exchange=EXCHANGE, interval=api_interval,
                                   start_date=start.strftime("%Y-%m-%d"),
                                   end_date=end.strftime("%Y-%m-%d"))
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        return df.reset_index()   # SDK returns timestamp as DatetimeIndex
    except Exception as e:
        log.warning(f"history({symbol} {interval}): {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS  (hand-rolled — no extra deps; VWAP resets daily, ADX is Wilder)
# ══════════════════════════════════════════════════════════════════════════════

def _normalise(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    dt_col = next((c for c in ("timestamp", "datetime", "date") if c in df.columns), None)
    if dt_col is None:
        return None
    df = df.copy()
    df[dt_col] = pd.to_datetime(df[dt_col], utc=False)
    if getattr(df[dt_col].dt, "tz", None) is not None:
        df[dt_col] = df[dt_col].dt.tz_localize(None)
    df = df.sort_values(dt_col).reset_index(drop=True)
    df["_dt"] = df[dt_col]
    df["_date"] = df[dt_col].dt.strftime("%Y-%m-%d")
    df["_hm"] = df[dt_col].dt.hour * 60 + df[dt_col].dt.minute
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Intraday VWAP, reset each day (typical price x volume, cumulative)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    cum_pv = pv.groupby(df["_date"]).cumsum()
    cum_v = df["volume"].groupby(df["_date"]).cumsum().replace(0, np.nan)
    df["vwap"] = (cum_pv / cum_v).astype(float)
    return df


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder ADX (approximated with ewm alpha=1/period)."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    up = h.diff()
    dn = -l.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    alpha = 1.0 / period
    tr = tr.astype(float)
    plus_dm = plus_dm.astype(float)
    minus_dm = minus_dm.astype(float)
    atr = tr.ewm(alpha=alpha, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean().astype(float)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL  (Pine buy1/buy2/sell1/sell2 + VWAP + ADX, on the just-closed bar)
# ══════════════════════════════════════════════════════════════════════════════

def _hm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[2:])


SESSION_START_MIN = _hm(SESSION_START_HM)
SESSION_END_MIN   = EXIT_HOUR * 60 + EXIT_MINUTE
ENTRY_CUTOFF_MIN  = _hm(ENTRY_CUTOFF_HM)


def evaluate_signal(df5: pd.DataFrame, today: str) -> Optional[dict]:
    """
    Evaluate the Pine breakout on the LAST CLOSED bar of today's session.
    Returns {direction, entry_ref, sl, signal_ts, adx, vwap} or None.

    Drops the final (forming) bar and evaluates on iloc[-1] of what remains,
    so we never repaint or fire mid-bar.
    """
    df = _normalise(df5)
    if df is None or df.empty:
        return None
    df = df[(df["_date"] == today) &
            (df["_hm"] >= SESSION_START_MIN) & (df["_hm"] <= SESSION_END_MIN)]
    if len(df) < 4:
        return None

    df = add_vwap(df)
    df = add_adx(df, ADX_LEN)

    # Drop the still-forming last bar -> evaluate the just-closed one
    df = df.iloc[:-1]
    if len(df) < 3:
        return None

    b  = df.iloc[-1]   # closed signal bar
    p1 = df.iloc[-2]
    p2 = df.iloc[-3]

    # Only generate within the entry window
    if not (SESSION_START_MIN <= int(b["_hm"]) <= ENTRY_CUTOFF_MIN):
        return None

    o, h, c, lo = float(b["open"]), float(b["high"]), float(b["close"]), float(b["low"])
    o1, h1, c1, l1 = float(p1["open"]), float(p1["high"]), float(p1["close"]), float(p1["low"])
    o2, h2, c2, l2 = float(p2["open"]), float(p2["high"]), float(p2["close"]), float(p2["low"])

    is_green, is_red = c > o, c < o
    prev_green, prev_red = c1 > o1, c1 < o1
    prev2_green, prev2_red = c2 > o2, c2 < o2

    buy1  = ENABLE_SINGLE and ENABLE_BUY  and is_green and prev_red   and c > h1
    sell1 = ENABLE_SINGLE and ENABLE_SELL and is_red   and prev_green and c < l1
    buy2  = ENABLE_DOUBLE and ENABLE_BUY  and is_green and prev_green and prev2_red   and c > h2
    sell2 = ENABLE_DOUBLE and ENABLE_SELL and is_red   and prev_red   and prev2_green and c < l2

    vwap = float(b["vwap"]) if pd.notna(b["vwap"]) else None
    adx  = float(b["adx"]) if pd.notna(b["adx"]) else 0.0

    vwap_buy_ok  = (not USE_VWAP) or (vwap is not None and c > vwap)
    vwap_sell_ok = (not USE_VWAP) or (vwap is not None and c < vwap)
    adx_ok = (not USE_ADX) or (adx >= ADX_THRESHOLD)

    if (buy1 or buy2) and vwap_buy_ok and adx_ok:
        sl = min(lo, l1) if buy1 else min(lo, l1, l2)
        return {"direction": "LONG", "entry_ref": c, "sl": sl,
                "signal_ts": str(b["_dt"]), "adx": adx, "vwap": vwap,
                "kind": "single" if buy1 else "double"}

    if (sell1 or sell2) and vwap_sell_ok and adx_ok:
        sl = max(h, h1) if sell1 else max(h, h1, h2)
        return {"direction": "SHORT", "entry_ref": c, "sl": sl,
                "signal_ts": str(b["_dt"]), "adx": adx, "vwap": vwap,
                "kind": "single" if sell1 else "double"}

    return None


# ══════════════════════════════════════════════════════════════════════════════
# SIZING  (Pine risk calculator: qty = risk_budget / stop_distance, notional-capped)
# ══════════════════════════════════════════════════════════════════════════════

def compute_qty(entry: float, sl: float) -> int:
    sl_dist = abs(entry - sl)
    if sl_dist < 0.01 or entry <= 0:
        return 0
    risk_budget = BALANCE * (MAX_LOSS_PCT / 100.0)        # e.g. 311000 * 1% = 3110
    qty_risk = math.floor(risk_budget / sl_dist)          # risk constraint
    qty_cap = math.floor(CAPITAL_PER_TRADE / entry)       # notional constraint
    return max(0, min(qty_risk, qty_cap))


# ══════════════════════════════════════════════════════════════════════════════
# CHARGES  (Zerodha intraday equity — for the EOD summary)
# ══════════════════════════════════════════════════════════════════════════════

def compute_charges(entry: float, exit_p: float, qty: int) -> float:
    buy_v, sell_v = entry * qty, exit_p * qty
    total = buy_v + sell_v
    brok = min(20.0, buy_v * 0.0003) + min(20.0, sell_v * 0.0003)
    stt = sell_v * 0.00025
    txn = total * 0.0000307
    sebi = total * 0.000001
    stamp = buy_v * 0.00003
    gst = (brok + sebi + txn) * 0.18
    return brok + stt + txn + sebi + stamp + gst


# ══════════════════════════════════════════════════════════════════════════════
# SEBI COMPLIANCE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, delay: float = ORDER_DELAY_S):
        self._delay, self._last = delay, 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last = time.monotonic()


_rate_limiter = RateLimiter()


def _pre_trade_check(symbol, direction, qty, entry, sl) -> tuple[bool, str]:
    sl_dist = abs(entry - sl)
    if sl_dist < 0.01:
        return False, "SL distance < 0.01 (degenerate candle)"
    if direction == "LONG" and entry <= sl:
        return False, f"LONG entry {entry:.2f} <= SL {sl:.2f}"
    if direction == "SHORT" and entry >= sl:
        return False, f"SHORT entry {entry:.2f} >= SL {sl:.2f}"
    if qty <= 0:
        return False, "qty = 0"
    if qty * entry > CAPITAL_PER_TRADE * 1.05:
        return False, f"capital Rs.{qty*entry:,.0f} exceeds limit Rs.{CAPITAL_PER_TRADE:,.0f}"
    sl_pct = (sl_dist / entry) * 100
    if MIN_SL_DIST_PCT > 0 and sl_pct < MIN_SL_DIST_PCT:
        return False, f"SL distance {sl_pct:.2f}% < MIN_SL_DIST_PCT {MIN_SL_DIST_PCT}% (charges > profit)"
    return True, "OK"


# ══════════════════════════════════════════════════════════════════════════════
# ORDER PLUMBING
# ══════════════════════════════════════════════════════════════════════════════

def _place_order(symbol, action, qty, price_type="MARKET", price=0.0, trigger_price=0.0):
    _rate_limiter.wait()
    if DRY_RUN:
        fake = f"DRY-{symbol}-{action}-{int(time.time())}"
        log.info(f"[DRY] {action} {qty} {symbol} {price_type} tp={trigger_price:.2f} -> {fake}")
        return fake
    try:
        r = _get_client().placeorder(strategy=STRATEGY_NAME, symbol=symbol, exchange=EXCHANGE,
                                     action=action, quantity=qty, price_type=price_type,
                                     product=PRODUCT, price=price, trigger_price=trigger_price)
        if r.get("status") == "success":
            log.info(f"ORDER {action} {qty} {symbol} {price_type} tp={trigger_price:.2f} -> {r['orderid']}")
            return r["orderid"]
        log.error(f"Order FAIL {action} {symbol}: {r}")
    except Exception as e:
        log.error(f"placeorder exception {symbol}: {e}")
    return None


def _modify_sl(order_id, trigger_price, symbol, sl_action, qty) -> bool:
    if DRY_RUN:
        log.info(f"[DRY] MODIFY {order_id} {symbol} -> tp={trigger_price:.2f}")
        return True
    try:
        r = _get_client().modifyorder(order_id=order_id, strategy=STRATEGY_NAME, symbol=symbol,
                                      action=sl_action, exchange=EXCHANGE, price_type="SL-M",
                                      product=PRODUCT, quantity=qty, price="0",
                                      trigger_price=str(round(trigger_price, 2)),
                                      disclosed_quantity="0")
        return r.get("status") == "success"
    except Exception as e:
        log.warning(f"modifyorder exception {order_id} ({symbol}): {e}")
        return False


def _cancel_order(order_id) -> bool:
    if DRY_RUN or not order_id or str(order_id).startswith("DRY-"):
        return True
    try:
        return _get_client().cancelorder(order_id=order_id, strategy=STRATEGY_NAME).get("status") == "success"
    except Exception as e:
        log.warning(f"cancelorder exception {order_id}: {e}")
        return False


def _order_status(order_id) -> Optional[str]:
    if not order_id or str(order_id).startswith("DRY-"):
        return None
    try:
        r = _get_client().orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
        if r.get("status") != "success":
            return None
        s = str((r.get("data") or {}).get("order_status") or "").lower()
        if "complete" in s or "filled" in s:
            return "complete"
        if "cancel" in s:
            return "cancelled"
        if "reject" in s:
            return "rejected"
        return "open"
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# POSITION + STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol: str
    direction: str            # LONG | SHORT
    entry_price: float
    qty: int
    initial_sl: float
    current_sl: float
    target: float             # 0.0 = no fixed target (time-exit only)
    entry_order_id: str
    sl_order_id: Optional[str]
    active: bool = True
    exit_price: float = 0.0
    exit_reason: str = ""
    reached_1r: bool = False
    reached_be: bool = False  # breakeven SL already activated
    # Signal metadata — stored for trade log
    signal_time: str = ""     # closed candle that fired the pattern (YYYY-MM-DD HH:MM:SS)
    entry_time: str = ""      # when the entry order was placed (HH:MM:SS IST)
    signal_adx: float = 0.0
    signal_vwap: float = 0.0
    kind: str = ""            # "single" | "double"


def _save_state(positions: dict, phase: str):
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump({"date": _RUN_DATE, "phase": phase,
                        "saved_at": _now_ist().strftime("%H:%M:%S"),
                        "positions": [asdict(p) for p in positions.values()]}, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        log.warning(f"_save_state failed: {e}")


def _load_state() -> Optional[dict]:
    if not os.path.exists(_STATE_FILE):
        return None
    try:
        with open(_STATE_FILE) as f:
            data = _json.load(f)
        return data if data.get("date") == _RUN_DATE else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now_ist() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def _cur_min() -> int:
    n = _now_ist()
    return n.hour * 60 + n.minute


# ══════════════════════════════════════════════════════════════════════════════
# POSITION MANAGEMENT  (trail / target / SL-hit detection)
# ══════════════════════════════════════════════════════════════════════════════

def _manage_position(pos: Position, today: str):
    """Per loop: detect SL fill, breakeven SL, trail SL, exit at target or time."""
    if not pos.active:
        return

    # 1. Did the broker SL-M already fill?
    if pos.sl_order_id:
        is_dry_sl = str(pos.sl_order_id).startswith("DRY-")
        broker_filled = (not is_dry_sl) and _order_status(pos.sl_order_id) == "complete"

        # DRY_RUN: simulate SL hit by checking if price breached the SL level
        dry_hit = False
        if is_dry_sl:
            df_sl = fetch_history(pos.symbol, INTERVAL, 1)
            if df_sl is not None:
                d_sl = _normalise(df_sl)
                if d_sl is not None:
                    d_sl = d_sl[d_sl["_date"] == today]
                    if not d_sl.empty:
                        bar_lo = float(d_sl.iloc[-1]["low"])
                        bar_hi = float(d_sl.iloc[-1]["high"])
                        dry_hit = (pos.direction == "LONG" and bar_lo <= pos.current_sl) or \
                                  (pos.direction == "SHORT" and bar_hi >= pos.current_sl)

        if broker_filled or dry_hit:
            pos.exit_price  = pos.current_sl
            pos.exit_reason = "SL_HIT"
            pos.active      = False
            log.info(f"  {'[DRY] ' if dry_hit else ''}SL HIT {pos.symbol} @ {pos.current_sl:.2f}")
            _jlog("SL_HIT", symbol=pos.symbol, exit=pos.current_sl, qty=pos.qty, dry=dry_hit)
            _write_trade_csv(pos)
            return

    df = fetch_history(pos.symbol, INTERVAL, 1)
    if df is None:
        return
    d = _normalise(df)
    if d is None:
        return
    d = d[d["_date"] == today]
    if d.empty:
        return
    last   = d.iloc[-1]               # latest (forming) bar — fine for LTP-ish read
    closed = d.iloc[-2] if len(d) >= 2 else last
    ltp    = float(last["close"])

    # 2. Fixed target hit → exit MARKET  (skipped when TARGET_RR=0)
    if pos.target > 0:
        hit_t = (pos.direction == "LONG" and ltp >= pos.target) or \
                (pos.direction == "SHORT" and ltp <= pos.target)
        if hit_t:
            if pos.sl_order_id:
                _cancel_order(pos.sl_order_id)
                time.sleep(0.2)
            exit_action = "SELL" if pos.direction == "LONG" else "BUY"
            _place_order(pos.symbol, exit_action, pos.qty, "MARKET")
            pos.exit_price, pos.exit_reason, pos.active = ltp, "TARGET", False
            log.info(f"  TARGET {pos.symbol} @ {ltp:.2f}")
            _jlog("TARGET", symbol=pos.symbol, exit=ltp, qty=pos.qty)
            _write_trade_csv(pos)
            return

    # 3. Breakeven SL — move SL to entry once price moves BREAKEVEN_PCT% in our favour
    if BREAKEVEN_PCT > 0 and not pos.reached_be and pos.sl_order_id:
        be_triggered = (
            pos.direction == "LONG"  and ltp >= pos.entry_price * (1 + BREAKEVEN_PCT / 100)
        ) or (
            pos.direction == "SHORT" and ltp <= pos.entry_price * (1 - BREAKEVEN_PCT / 100)
        )
        if be_triggered:
            sl_action = "SELL" if pos.direction == "LONG" else "BUY"
            # For LONG: move SL up to entry; for SHORT: move SL down to entry
            be_sl = pos.entry_price
            if _modify_sl(pos.sl_order_id, be_sl, pos.symbol, sl_action, pos.qty):
                log.info(f"  BREAKEVEN {pos.symbol} SL {pos.current_sl:.2f} → {be_sl:.2f} (entry, free trade)")
                _jlog("BREAKEVEN", symbol=pos.symbol, old_sl=pos.current_sl, new_sl=be_sl)
                pos.current_sl = be_sl
                pos.reached_be = True

    # 4. Trail SL on the last CLOSED candle (mode: none | after_1R | full)
    if TRAIL_MODE != "none" and pos.sl_order_id:
        dist = abs(pos.entry_price - pos.initial_sl)
        # mark +1R favourable excursion (intrabar) for after_1R mode
        if not pos.reached_1r and dist > 0:
            if (pos.direction == "LONG" and float(last["high"]) >= pos.entry_price + dist) or \
               (pos.direction == "SHORT" and float(last["low"]) <= pos.entry_price - dist):
                pos.reached_1r = True
        if TRAIL_MODE == "full":
            allow = (pos.direction == "LONG" and float(closed["close"]) > pos.entry_price) or \
                    (pos.direction == "SHORT" and float(closed["close"]) < pos.entry_price)
        else:  # after_1R
            allow = pos.reached_1r
        if allow:
            sl_action = "SELL" if pos.direction == "LONG" else "BUY"
            if pos.direction == "LONG":
                new_sl = max(pos.current_sl, float(closed["low"]))
                improved = new_sl > pos.current_sl + 0.01
            else:
                new_sl = min(pos.current_sl, float(closed["high"]))
                improved = new_sl < pos.current_sl - 0.01
            if improved and _modify_sl(pos.sl_order_id, new_sl, pos.symbol, sl_action, pos.qty):
                log.info(f"  TRAIL {pos.symbol} SL {pos.current_sl:.2f} -> {new_sl:.2f}")
                _jlog("TRAIL", symbol=pos.symbol, new_sl=new_sl)
                pos.current_sl = new_sl


def _square_off_all(positions: dict, today: str):
    log.info("Square-off — flattening all open positions")
    for pos in positions.values():
        if not pos.active:
            continue
        if pos.sl_order_id and _order_status(pos.sl_order_id) == "complete":
            pos.exit_price, pos.exit_reason, pos.active = pos.current_sl, "SL_HIT", False
            _write_trade_csv(pos)
            continue
        if pos.sl_order_id:
            _cancel_order(pos.sl_order_id)
            time.sleep(0.2)
        exit_action = "SELL" if pos.direction == "LONG" else "BUY"
        _place_order(pos.symbol, exit_action, pos.qty, "MARKET")
        df = fetch_history(pos.symbol, INTERVAL, 1)
        if df is not None:
            d = _normalise(df)
            if d is not None and not d[d["_date"] == today].empty:
                pos.exit_price = float(d[d["_date"] == today].iloc[-1]["close"])
        pos.exit_reason, pos.active = pos.exit_reason or "TIME_EXIT", False
        _jlog("SQUAREOFF", symbol=pos.symbol, exit=pos.exit_price, qty=pos.qty)
        _write_trade_csv(pos)
        time.sleep(0.3)
    _save_state(positions, "DONE")


def _daily_loss_ok(positions: dict) -> bool:
    # Closed P&L
    realised = sum((p.exit_price - p.entry_price) * p.qty * (1 if p.direction == "LONG" else -1)
                   for p in positions.values() if not p.active and p.exit_price > 0)
    # Unrealized: use current_sl as worst-case proxy for open positions
    unrealised = sum((p.current_sl - p.entry_price) * p.qty * (1 if p.direction == "LONG" else -1)
                     for p in positions.values() if p.active)
    total = realised + unrealised
    if total < -MAX_DAILY_LOSS_RS:
        log.error(f"DAILY LOSS LIMIT HIT: realised=Rs.{realised:,.0f} worst-case=Rs.{total:,.0f} "
                  f"(limit Rs.{MAX_DAILY_LOSS_RS:,.0f})")
        return False
    return True


def _print_summary(positions: dict):
    log.info("=" * 96)
    log.info("  BREAKOUT INTRADAY — TRADE SUMMARY")
    log.info(f"  Date={_RUN_DATE}  DRY_RUN={DRY_RUN}  adx>={ADX_THRESHOLD}  trail={TRAIL_MODE}  "
             f"be={BREAKEVEN_PCT}%  target={'{}R'.format(TARGET_RR) if TARGET_RR>0 else 'time'}  "
             f"exit={EXIT_TIME_HM}  reentry={ALLOW_REENTRY}")
    log.info("=" * 96)
    log.info(f"{'SYMBOL':<12} {'DIR':<6} {'KIND':<7} {'SIG_T':<6} {'ENTRY':>9} {'EXIT':>9} "
             f"{'QTY':>5} {'GROSS':>9} {'CHRG':>8} {'NET':>9}  {'REASON':<10} BE  1R")
    tg = tn = tc = 0.0
    wins = losses = 0
    by_reason: dict = {}
    for p in sorted(positions.values(), key=lambda x: x.signal_time):
        if p.exit_price == 0.0:
            continue
        gross = (p.exit_price - p.entry_price) * p.qty * (1 if p.direction == "LONG" else -1)
        ch    = compute_charges(p.entry_price, p.exit_price, p.qty)
        net   = gross - ch
        tg   += gross;  tn += net;  tc += ch
        wins += 1 if net > 0 else 0
        losses += 1 if net <= 0 else 0
        by_reason[p.exit_reason] = by_reason.get(p.exit_reason, 0) + 1
        sig_t = p.signal_time[11:16] if len(p.signal_time) >= 16 else p.signal_time
        log.info(f"{p.symbol:<12} {p.direction:<6} {p.kind:<7} {sig_t:<6} "
                 f"{p.entry_price:>9.2f} {p.exit_price:>9.2f} "
                 f"{p.qty:>5} {gross:>+9.0f} {ch:>8.0f} {net:>+9.0f}  "
                 f"{p.exit_reason:<10} {'Y' if p.reached_be else 'N'}   {'Y' if p.reached_1r else 'N'}")
    n = wins + losses
    log.info("-" * 96)
    log.info(f"{'TOTAL':<12} {'':6} {'':7} {'':6} {'':>9} {'':>9} "
             f"{'':>5} {tg:>+9.0f} {tc:>8.0f} {tn:>+9.0f}")
    log.info(f"  Trades={n}  Wins={wins}({int(wins/n*100) if n else 0}%)  Losses={losses}  "
             f"Charges=Rs.{tc:,.0f}  Exits: {by_reason}")
    log.info(f"  Log files: {_LOG_DIR}/breakout_*_{_RUN_DATE}.*")
    log.info("=" * 96)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LIVE LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_live():
    log.info("=" * 84)
    log.info(f"  BREAKOUT INTRADAY — starting   Exchange={EXCHANGE}  Interval={INTERVAL}  DRY_RUN={DRY_RUN}")
    log.info(f"  ── SIGNAL  single={ENABLE_SINGLE} double={ENABLE_DOUBLE}  "
             f"buy={ENABLE_BUY} sell={ENABLE_SELL}  VWAP={USE_VWAP}  ADX={USE_ADX}>={ADX_THRESHOLD}(len={ADX_LEN})")
    tgt_str = f"{TARGET_RR}R" if TARGET_RR > 0 else "NONE(time-exit)"
    be_str  = f"{BREAKEVEN_PCT}%" if BREAKEVEN_PCT > 0 else "off"
    log.info(f"  ── EXIT    Target={tgt_str}  Trail={TRAIL_MODE}  Breakeven={be_str}  "
             f"ExitTime={EXIT_TIME_HM}  HardExit={HARD_EXIT_TIME_HM}")
    log.info(f"  ── SIZING  Balance=Rs.{BALANCE:,.0f}  Risk={MAX_LOSS_PCT}%/trade=Rs.{BALANCE*MAX_LOSS_PCT/100:,.0f}  "
             f"NotionalCap=Rs.{CAPITAL_PER_TRADE:,.0f}")
    log.info(f"  ── SESSION {SESSION_START_HM}-{EXIT_TIME_HM}  EntryCutoff={ENTRY_CUTOFF_HM}  "
             f"MaxPos={MAX_OPEN_POSITIONS}  MaxDailyLoss=Rs.{MAX_DAILY_LOSS_RS:,.0f}  "
             f"Reentry={ALLOW_REENTRY}")
    log.info("=" * 84)
    _jlog("STARTUP", dry_run=DRY_RUN, exchange=EXCHANGE, interval=INTERVAL,
          risk_per_trade=BALANCE * MAX_LOSS_PCT / 100, target_rr=TARGET_RR)

    today = _now_ist().strftime("%Y-%m-%d")
    positions: dict[str, Position] = {}

    # Resume open positions after a restart
    saved = _load_state()
    if saved and saved.get("phase") == "MONITORING":
        for pd_ in saved.get("positions", []):
            positions[pd_["symbol"]] = Position(**pd_)
        log.info(f"RECOVERY — resumed {len(positions)} position(s) from earlier run")

    watchlist: list[dict] = []
    last_wl_fetch = 0.0
    last_bar_ts: dict[str, str] = {}      # per-symbol last evaluated closed-bar timestamp
    hard_exit_min = HARD_EXIT_HOUR * 60 + HARD_EXIT_MINUTE

    while not stop_event.is_set():
        cm = _cur_min()

        # Before session — idle wait
        if cm < SESSION_START_MIN:
            log.info(f"Pre-session ({_now_ist():%H:%M}); waiting for {SESSION_START_HM}")
            stop_event.wait(min(60, (SESSION_START_MIN - cm) * 60))
            continue

        # Session over — square off and finish
        if cm >= SESSION_END_MIN or cm >= hard_exit_min:
            _square_off_all(positions, today)
            break

        if not _daily_loss_ok(positions):
            _square_off_all(positions, today)
            break

        # 1. Refresh TradeFinder watchlist (throttled)
        if time.time() - last_wl_fetch >= WATCHLIST_REFRESH_SEC:
            wl = fetch_intraday_boost()
            if wl:
                watchlist = wl[:MAX_WATCHLIST] if MAX_WATCHLIST > 0 else wl
                log.info(f"[{_now_ist():%H:%M:%S}] Watchlist: {len(watchlist)} stocks "
                         f"(top: {', '.join(s['symbol'] for s in watchlist[:5])})")
                _jlog("WATCHLIST", count=len(watchlist), top5=[s["symbol"] for s in watchlist[:5]])
            elif watchlist:
                log.warning(f"[{_now_ist():%H:%M:%S}] ⚠ TradeFinder fetch failed — "
                            f"using stale watchlist ({len(watchlist)} stocks). JWT expired?")
            else:
                log.error(f"[{_now_ist():%H:%M:%S}] ✗ No watchlist — TF fetch failed and no prior list. "
                          f"Set TF_JWT_TOKEN to a fresh token.")
            last_wl_fetch = time.time()

        entries_open = cm <= ENTRY_CUTOFF_MIN

        # 2. Scan watchlist for fresh signals
        for stock in watchlist:
            if stop_event.is_set():
                break
            sym = stock["symbol"]
            # Skip if position already open on this symbol
            if sym in positions and positions[sym].active:
                continue
            # If re-entry disabled, skip symbols that already traded today
            if not ALLOW_REENTRY and sym in positions:
                continue
            if not entries_open or len([p for p in positions.values() if p.active]) >= MAX_OPEN_POSITIONS:
                continue

            df = fetch_history(sym, INTERVAL, 2)
            if df is None:
                continue
            sig = evaluate_signal(df, today)
            if not sig:
                continue
            if last_bar_ts.get(sym) == sig["signal_ts"]:
                continue                              # already acted on this bar
            last_bar_ts[sym] = sig["signal_ts"]

            entry_ref, sl, direction = sig["entry_ref"], sig["sl"], sig["direction"]
            qty = compute_qty(entry_ref, sl)
            ok, reason = _pre_trade_check(sym, direction, qty, entry_ref, sl)
            if not ok:
                log.info(f"  {sym:12s} {direction} skip: {reason}")
                continue

            sl_dist = abs(entry_ref - sl)
            # TARGET_RR=0 → no fixed target, hold till EXIT_TIME or SL
            target = (entry_ref + TARGET_RR * sl_dist if direction == "LONG"
                      else entry_ref - TARGET_RR * sl_dist) if TARGET_RR > 0 else 0.0
            tgt_str = f"{target:.2f}" if target > 0 else "NONE(time-exit)"
            log.info(f"  SIGNAL {sym:12s} {direction} [{sig['kind']}] entry~{entry_ref:.2f} "
                     f"SL={sl:.2f} T={tgt_str} qty={qty} adx={sig['adx']:.1f} "
                     f"be={BREAKEVEN_PCT}%")
            _jlog("SIGNAL", symbol=sym, direction=direction, kind=sig["kind"], entry=entry_ref,
                  sl=sl, target=target, qty=qty, adx=round(sig["adx"], 2))

            # Entry MARKET + broker-side SL-M
            entry_action = "BUY" if direction == "LONG" else "SELL"
            entry_id = _place_order(sym, entry_action, qty, "MARKET")
            if not entry_id:
                continue
            sl_action = "SELL" if direction == "LONG" else "BUY"
            sl_id = _place_order(sym, sl_action, qty, "SL-M", trigger_price=round(sl, 2))
            if not sl_id:
                log.error(f"  ⚠ SL ORDER FAILED for {sym} — cancelling entry to avoid naked position")
                _cancel_order(entry_id)
                continue
            positions[sym] = Position(
                symbol=sym, direction=direction, entry_price=entry_ref,
                qty=qty, initial_sl=sl, current_sl=sl, target=target,
                entry_order_id=entry_id, sl_order_id=sl_id,
                signal_time=sig["signal_ts"],
                entry_time=_now_ist().strftime("%H:%M:%S"),
                signal_adx=round(sig["adx"], 2),
                signal_vwap=round(sig["vwap"] or 0.0, 2), kind=sig["kind"],
            )
            _jlog("POSITION_OPEN", symbol=sym, direction=direction, entry=entry_ref,
                  sl=sl, target=target, qty=qty, kind=sig["kind"], adx=round(sig["adx"], 2))
            _save_state(positions, "MONITORING")

        # 3. Manage open positions
        for pos in list(positions.values()):
            if stop_event.is_set():
                break
            _manage_position(pos, today)
        _save_state(positions, "MONITORING")

        active = sum(1 for p in positions.values() if p.active)
        log.info(f"[{_now_ist():%H:%M:%S}] heartbeat — watchlist={len(watchlist)} active_pos={active}")
        stop_event.wait(POLL_INTERVAL_SEC)

    _print_summary(positions)
    try:
        _get_client().disconnect()
    except Exception:
        pass
    log.info("Shutdown complete")


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS  (python breakout_intraday_strategy.py --test)
# ══════════════════════════════════════════════════════════════════════════════

def run_diagnostics():
    today = _now_ist().strftime("%Y-%m-%d")
    log.info("=" * 72)
    log.info("  BREAKOUT INTRADAY — DIAGNOSTICS")
    log.info(f"  Date={today}  Interval={INTERVAL}  VWAP={USE_VWAP} ADX={USE_ADX}>={ADX_THRESHOLD}")
    log.info("=" * 72)

    log.info("\nSTEP 1 — TradeFinder IntradayBoost")
    stocks = fetch_intraday_boost()
    if not stocks:
        log.error("  Could not fetch stocks. Check TF_JWT_TOKEN / network.")
        return
    log.info(f"  Received {len(stocks)} stocks. Top 10:")
    for i, s in enumerate(stocks[:10], 1):
        log.info(f"   {i:>2}  {s['symbol']:<14} score={s['score']:>7.3f} ltp={s['ltp']:>9.2f} chg={s['change_pct']:>+6.2f}%")

    log.info(f"\nSTEP 2 — signal check on first {min(10, len(stocks))} stocks")
    for stock in stocks[:10]:
        sym = stock["symbol"]
        df = fetch_history(sym, INTERVAL, 2)
        if df is None:
            log.info(f"  {sym:12s}  no data")
            continue
        sig = evaluate_signal(df, today)
        if sig:
            qty = compute_qty(sig["entry_ref"], sig["sl"])
            sl_dist = abs(sig["entry_ref"] - sig["sl"])
            tgt = sig["entry_ref"] + TARGET_RR * sl_dist if sig["direction"] == "LONG" else sig["entry_ref"] - TARGET_RR * sl_dist
            log.info(f"  {sym:12s}  {sig['direction']} [{sig['kind']}] entry~{sig['entry_ref']:.2f} "
                     f"SL={sig['sl']:.2f} T={tgt:.2f} qty={qty} adx={sig['adx']:.1f}")
        else:
            log.info(f"  {sym:12s}  no signal on last closed {INTERVAL} bar")
        time.sleep(0.2)
    log.info("\nDiagnostics complete.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="one diagnostic pass, no orders")
    p.add_argument("--mode", choices=["live"], default=os.getenv("MODE", "live"))
    args = p.parse_args()
    if args.test or os.getenv("TEST_MODE", "false").lower() == "true":
        run_diagnostics()
    else:
        run_live()


if __name__ == "__main__":
    main()
