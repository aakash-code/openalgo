#!/usr/bin/env python3
"""
================================================================================
  INTRADAY BREAKOUT SCANNER  —  OpenAlgo + TradeFinder
================================================================================
  Standalone scanner. No orders placed. Pure signal detection.

  Pine Script port: "Intraday Signals & Risk Calculator [India v5.6]"
  ─────────────────────────────────────────────────────────────────────────────
  Source   : TradeFinder IntradayBoost  (refreshed every WATCHLIST_REFRESH_SEC)
  Signal   : double-candle breakout  (buy1/buy2/sell1/sell2)
  Filters  : VWAP  +  ADX >= threshold
  Interval : scan every SCAN_INTERVAL_SEC  on SCAN_INTERVAL bars (default 1m)
  Output   : console  +  logs/scanner/scanner_YYYY-MM-DD.log  +  CSV
  Telegram : set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID for push alerts

  Usage:
      python breakout_scanner.py               # live loop
      python breakout_scanner.py --test        # single pass, then exit

  Env vars (all optional, sensible defaults):
      OPENALGO_API_KEY, HOST_SERVER, EXCHANGE
      TF_JWT_TOKEN  or  TF_JWT_FILE  (default: strategies/tf_jwt.txt)
      SCAN_INTERVAL_SEC=60   SCAN_INTERVAL=1m   MAX_WORKERS=10
      WATCHLIST_REFRESH_SEC=300   MAX_WATCHLIST=0
      ADX_THRESHOLD=25   USE_VWAP=true   USE_ADX=true
      TELEGRAM_BOT_TOKEN   TELEGRAM_CHAT_ID
================================================================================
"""

import argparse
import base64
import hashlib
import hmac
import logging
import os
import signal
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
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
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

OPENALGO_API_KEY = os.getenv("OPENALGO_API_KEY", "")
OPENALGO_HOST    = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
EXCHANGE         = os.getenv("EXCHANGE", "NSE")

# TradeFinder JWT — hot-reloadable: paste fresh token to tf_jwt.txt without restart
TF_JWT_TOKEN          = os.getenv("TF_JWT_TOKEN", "")
TF_JWT_FILE           = os.getenv("TF_JWT_FILE", os.path.join(os.path.dirname(__file__), "tf_jwt.txt"))
WATCHLIST_REFRESH_SEC = int(os.getenv("WATCHLIST_REFRESH_SEC", "300"))   # refresh list every 5 min
MAX_WATCHLIST         = int(os.getenv("MAX_WATCHLIST", "0"))             # 0 = all; else top-N by score

# Scanner
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))    # run scan every N seconds
SCAN_INTERVAL     = os.getenv("SCAN_INTERVAL", "1m")             # bar timeframe (1m, 5m, 15m)
MAX_WORKERS       = int(os.getenv("MAX_WORKERS", "10"))           # parallel fetches

# Session window (IST, HHMM)
SESSION_START_HM = os.getenv("SESSION_START_HM", "0915")
SESSION_END_HM   = os.getenv("SESSION_END_HM",   "1525")
ENTRY_CUTOFF_HM  = os.getenv("ENTRY_CUTOFF_HM",  "1500")   # no new signals after this

# Signal filters (defaults match Pine v5.6 best-performing config)
ENABLE_BUY    = os.getenv("ENABLE_BUY",    "true").lower()  == "true"
ENABLE_SELL   = os.getenv("ENABLE_SELL",   "true").lower()  == "true"
ENABLE_SINGLE = os.getenv("ENABLE_SINGLE", "false").lower() == "true"   # single-candle (weaker)
ENABLE_DOUBLE = os.getenv("ENABLE_DOUBLE", "true").lower()  == "true"   # double-candle (sweep winner)
USE_VWAP      = os.getenv("USE_VWAP",  "true").lower() == "true"
USE_ADX       = os.getenv("USE_ADX",   "true").lower() == "true"
ADX_LEN       = int(os.getenv("ADX_LEN", "14"))
ADX_THRESHOLD = float(os.getenv("ADX_THRESHOLD", "25"))

# Telegram (optional)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Output dir
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR   = os.path.join(_BASE_DIR, "logs", "scanner")


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def _setup_logging() -> logging.Logger:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"scanner_{date.today():%Y-%m-%d}.log")
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
    logger = logging.getLogger("scanner")
    logger.setLevel(logging.INFO)
    for handler in [logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)]:
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


log = _setup_logging()


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _hm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[2:])


SESSION_START_MIN = _hm(SESSION_START_HM)
SESSION_END_MIN   = _hm(SESSION_END_HM)
ENTRY_CUTOFF_MIN  = _hm(ENTRY_CUTOFF_HM)


def _now() -> datetime:
    return datetime.now()


def _in_session() -> bool:
    n = _now()
    now_min = n.hour * 60 + n.minute
    return SESSION_START_MIN <= now_min <= SESSION_END_MIN


# ══════════════════════════════════════════════════════════════════════════════
# TRADEFINDER CLIENT  (verbatim from breakout_intraday_strategy.py)
# ══════════════════════════════════════════════════════════════════════════════

_TF_BASE   = "https://tradefinder.in/api_be"
_TF_SECRET = "5ACHPKZUZNTWYPSJXNP7IULMACAM6P6Q"

import urllib3  # noqa: E402
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_TF_SSL_VERIFY = False


def _get_tf_jwt() -> str:
    if TF_JWT_FILE and os.path.exists(TF_JWT_FILE):
        try:
            token = Path(TF_JWT_FILE).read_text().strip()
            if token:
                return token
        except Exception:
            pass
    return TF_JWT_TOKEN


def _generate_totp(ts_ms: int) -> str:
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
    """Fetch TradeFinder IntradayBoost list, sorted by score desc."""
    jwt = _get_tf_jwt()
    if not jwt:
        log.error("TF JWT empty — echo '<token>' > strategies/tf_jwt.txt")
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
        log.error(f"TradeFinder: {data.get('code')} — {data.get('message')} "
                  f"(JWT expired? paste a fresh token into tf_jwt.txt)")
        return []
    raw      = (data.get("payload") or {}).get("data") or {}
    raw_list = raw.get("intraday_boost") or raw.get("intradayBoost") or []
    items = [
        {"symbol": r["Symbol"], "ltp": float(r["param_0"]),
         "change_pct": float(r["param_2"]), "score": float(r["param_3"])}
        for r in raw_list
    ]
    items.sort(key=lambda x: x["score"], reverse=True)
    if MAX_WATCHLIST > 0:
        items = items[:MAX_WATCHLIST]
    return items


# ══════════════════════════════════════════════════════════════════════════════
# OPENALGO CLIENT + DATA
# ══════════════════════════════════════════════════════════════════════════════

_client: Optional[openalgo_api] = None


def _get_client() -> openalgo_api:
    global _client
    if _client is None:
        _client = openalgo_api(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)
    return _client


def fetch_history(symbol: str, interval: str, days: int = 1) -> Optional[pd.DataFrame]:
    end   = date.today()
    start = end - timedelta(days=days)
    api_interval = {"1d": "D", "1w": "W", "1M": "M"}.get(interval, interval)
    try:
        df = _get_client().history(
            symbol=symbol, exchange=EXCHANGE, interval=api_interval,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        return df.reset_index()
    except Exception as e:
        log.debug(f"history({symbol} {interval}): {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS  (hand-rolled; VWAP resets each day, ADX is Wilder smoothed)
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
    df["_dt"]   = df[dt_col]
    df["_date"] = df[dt_col].dt.strftime("%Y-%m-%d")
    df["_hm"]   = df[dt_col].dt.hour * 60 + df[dt_col].dt.minute
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    tp    = (df["high"] + df["low"] + df["close"]) / 3.0
    pv    = tp * df["volume"]
    cum_pv = pv.groupby(df["_date"]).cumsum()
    cum_v  = df["volume"].groupby(df["_date"]).cumsum().replace(0, np.nan)
    df["vwap"] = (cum_pv / cum_v).astype(float)
    return df


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c   = c.shift(1)
    tr       = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    up, dn   = h.diff(), -l.diff()
    plus_dm  = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    alpha    = 1.0 / period
    atr      = tr.astype(float).ewm(alpha=alpha, adjust=False).mean().replace(0, np.nan)
    plus_di  = 100 * plus_dm.astype(float).ewm(alpha=alpha, adjust=False).mean()  / atr
    minus_di = 100 * minus_dm.astype(float).ewm(alpha=alpha, adjust=False).mean() / atr
    di_sum   = (plus_di + minus_di).replace(0, np.nan)
    dx       = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean().astype(float)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL  (Pine buy1/buy2/sell1/sell2 + VWAP + ADX — identical to strategy)
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_signal(df_raw: pd.DataFrame, today: str) -> Optional[dict]:
    """
    Evaluate the Pine breakout on the last CLOSED bar of today's session.
    Drops the still-forming bar so signals never repaint mid-bar.
    Returns signal dict or None.
    """
    df = _normalise(df_raw)
    if df is None or df.empty:
        return None

    df = df[
        (df["_date"] == today) &
        (df["_hm"] >= SESSION_START_MIN) &
        (df["_hm"] <= SESSION_END_MIN)
    ]
    if len(df) < 4:
        return None

    df = add_vwap(df)
    df = add_adx(df, ADX_LEN)
    df = df.iloc[:-1]       # drop still-forming bar → evaluate the just-closed one
    if len(df) < 3:
        return None

    b  = df.iloc[-1]        # signal bar  (just closed)
    p1 = df.iloc[-2]        # prev bar
    p2 = df.iloc[-3]        # prev-prev bar

    if not (SESSION_START_MIN <= int(b["_hm"]) <= ENTRY_CUTOFF_MIN):
        return None

    o,  h,  c,  lo = float(b["open"]),  float(b["high"]),  float(b["close"]),  float(b["low"])
    o1, h1, c1, l1 = float(p1["open"]), float(p1["high"]), float(p1["close"]), float(p1["low"])
    o2, h2, c2, l2 = float(p2["open"]), float(p2["high"]), float(p2["close"]), float(p2["low"])

    is_green,  is_red   = c > o,   c < o
    prev_green, prev_red = c1 > o1, c1 < o1
    prev2_green, prev2_red = c2 > o2, c2 < o2

    buy1  = ENABLE_SINGLE and ENABLE_BUY  and is_green and prev_red   and c > h1
    sell1 = ENABLE_SINGLE and ENABLE_SELL and is_red   and prev_green and c < l1
    buy2  = ENABLE_DOUBLE and ENABLE_BUY  and is_green and prev_green and prev2_red   and c > h2
    sell2 = ENABLE_DOUBLE and ENABLE_SELL and is_red   and prev_red   and prev2_green and c < l2

    vwap = float(b["vwap"]) if pd.notna(b["vwap"]) else None
    adx  = float(b["adx"])  if pd.notna(b["adx"])  else 0.0

    vwap_buy_ok  = (not USE_VWAP) or (vwap is not None and c > vwap)
    vwap_sell_ok = (not USE_VWAP) or (vwap is not None and c < vwap)
    adx_ok       = (not USE_ADX)  or (adx >= ADX_THRESHOLD)

    # SL: Pine uses min of all breakout candle lows (buy) / max of highs (sell)
    sl_buy  = min(lo, l1) if buy1 else min(lo, l1, l2)
    sl_sell = max(h,  h1) if sell1 else max(h,  h1, h2)

    if (buy1 or buy2) and vwap_buy_ok and adx_ok:
        return {
            "direction": "BUY",  "entry": round(c, 2), "sl": round(sl_buy, 2),
            "signal_ts": str(b["_dt"]), "adx": round(adx, 1),
            "vwap": round(vwap or 0, 2),
            "kind": "double" if buy2 else "single",
        }
    if (sell1 or sell2) and vwap_sell_ok and adx_ok:
        return {
            "direction": "SELL", "entry": round(c, 2), "sl": round(sl_sell, 2),
            "signal_ts": str(b["_dt"]), "adx": round(adx, 1),
            "vwap": round(vwap or 0, 2),
            "kind": "double" if sell2 else "single",
        }
    return None


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def _send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.debug(f"Telegram: {e}")


def _telegram_text(sig: dict, sl_pct: float) -> str:
    icon = "🟢" if sig["direction"] == "BUY" else "🔴"
    return (
        f"{icon} <b>{sig['direction']} {sig['symbol']}</b>\n"
        f"Entry  : {sig['entry']:.2f}\n"
        f"SL     : {sig['sl']:.2f}  ({sl_pct:.2f}%)\n"
        f"ADX    : {sig['adx']:.1f}   VWAP: {sig['vwap']:.2f}\n"
        f"Kind   : {sig['kind']}\n"
        f"Bar    : {sig['signal_ts']}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CSV SIGNAL LOG
# ══════════════════════════════════════════════════════════════════════════════

_csv_path = ""
_csv_lock = threading.Lock()
_CSV_HEADER = "scan_time,symbol,direction,kind,entry,sl,sl_pct,adx,vwap,signal_ts\n"


def _init_csv() -> None:
    global _csv_path
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    _csv_path = os.path.join(LOG_DIR, f"signals_{date.today():%Y-%m-%d}.csv")
    if not os.path.exists(_csv_path):
        Path(_csv_path).write_text(_CSV_HEADER)


def _append_csv(row: dict) -> None:
    line = ",".join(str(row[k]) for k in
        ["scan_time", "symbol", "direction", "kind",
         "entry", "sl", "sl_pct", "adx", "vwap", "signal_ts"])
    with _csv_lock:
        with open(_csv_path, "a") as f:
            f.write(line + "\n")


# ══════════════════════════════════════════════════════════════════════════════
# PARALLEL SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def _scan_one(symbol: str, today: str) -> Optional[dict]:
    df = fetch_history(symbol, SCAN_INTERVAL, days=1)
    if df is None:
        return None
    sig = evaluate_signal(df, today)
    if sig:
        sig["symbol"] = symbol
    return sig


def run_scan(watchlist: list[dict]) -> list[dict]:
    """Scan all stocks in parallel. Returns signals sorted by symbol."""
    today   = date.today().strftime("%Y-%m-%d")
    symbols = [s["symbol"] for s in watchlist]
    signals = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_scan_one, sym, today): sym for sym in symbols}
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result:
                    signals.append(result)
            except Exception as e:
                log.debug(f"scan_one error: {e}")
    signals.sort(key=lambda x: x["symbol"])
    return signals


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _console_line(sig: dict, sl_pct: float) -> str:
    arrow = "▲ BUY " if sig["direction"] == "BUY" else "▼ SELL"
    return (
        f"  {arrow}  {sig['symbol']:<14}  "
        f"Entry:{sig['entry']:.2f}  SL:{sig['sl']:.2f} ({sl_pct:.2f}%)  "
        f"ADX:{sig['adx']:.1f}  VWAP:{sig['vwap']:.2f}  "
        f"[{sig['kind']}]  bar:{sig['signal_ts']}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def main(test_mode: bool = False) -> None:
    _init_csv()

    log.info("=" * 72)
    log.info("  INTRADAY BREAKOUT SCANNER")
    log.info(f"  Bar interval : {SCAN_INTERVAL}   Scan every : {SCAN_INTERVAL_SEC}s")
    log.info(f"  Filters      : VWAP={USE_VWAP}  ADX={USE_ADX} >={ADX_THRESHOLD}  "
             f"double={ENABLE_DOUBLE}  single={ENABLE_SINGLE}")
    log.info(f"  Session      : {SESSION_START_HM}–{SESSION_END_HM}  "
             f"Entry cutoff: {ENTRY_CUTOFF_HM}")
    log.info(f"  Signals CSV  : {_csv_path}")
    log.info("=" * 72)

    _stop = threading.Event()

    def _handle_sig(*_):
        log.info("Shutdown signal received.")
        _stop.set()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT,  _handle_sig)

    watchlist:    list[dict] = []
    last_wl_time: float      = 0.0
    # Deduplicate: symbol → "direction:signal_ts" to avoid re-alerting same bar
    seen: dict[str, str] = {}

    while not _stop.is_set():
        now    = _now()
        now_ts = time.time()

        # ── Refresh watchlist ────────────────────────────────────────────────
        if now_ts - last_wl_time >= WATCHLIST_REFRESH_SEC or not watchlist:
            fresh = fetch_intraday_boost()
            if fresh:
                watchlist     = fresh
                last_wl_time  = now_ts
                symbols_str   = ", ".join(s["symbol"] for s in watchlist[:10])
                more          = f" … +{len(watchlist)-10}" if len(watchlist) > 10 else ""
                log.info(f"Watchlist refreshed: {len(watchlist)} stocks  "
                         f"[{symbols_str}{more}]")
            elif not watchlist:
                log.warning("Watchlist empty — retrying in 30s. Check TF JWT.")
                _stop.wait(timeout=30)
                continue

        # ── Session check ────────────────────────────────────────────────────
        if not _in_session():
            if test_mode:
                log.info("Outside session — test mode forces a scan anyway.")
            else:
                log.info(f"Outside session ({now:%H:%M}) — sleeping 60s ...")
                _stop.wait(timeout=60)
                continue

        # ── Scan ─────────────────────────────────────────────────────────────
        t0 = time.time()
        log.info(f"[{now:%H:%M:%S}] Scanning {len(watchlist)} stocks ({SCAN_INTERVAL}) ...")

        signals  = run_scan(watchlist)
        elapsed  = time.time() - t0
        new_sigs = 0
        scan_ts  = now.strftime("%H:%M:%S")

        for sig in signals:
            sym     = sig["symbol"]
            sig_key = f"{sig['direction']}:{sig['signal_ts']}"

            if seen.get(sym) == sig_key:
                continue                # already alerted this exact bar signal

            seen[sym] = sig_key
            sl_pct    = round(abs(sig["entry"] - sig["sl"]) / sig["entry"] * 100, 2)

            log.info(_console_line(sig, sl_pct))
            _send_telegram(_telegram_text(sig, sl_pct))
            _append_csv({**sig, "scan_time": scan_ts, "sl_pct": sl_pct})
            new_sigs += 1

        log.info(
            f"[{now:%H:%M:%S}] Done in {elapsed:.1f}s — "
            f"{len(signals)} signal(s) found, {new_sigs} new"
        )

        if test_mode:
            break

        # ── Sleep to next scan boundary ──────────────────────────────────────
        sleep_sec = max(1.0, SCAN_INTERVAL_SEC - (time.time() - t0))
        _stop.wait(timeout=sleep_sec)

    log.info("Scanner stopped.")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Intraday Breakout Scanner")
    parser.add_argument("--test", action="store_true",
                        help="Run a single scan pass then exit (no session gate)")
    args = parser.parse_args()
    main(test_mode=args.test)
