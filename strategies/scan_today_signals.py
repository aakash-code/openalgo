#!/usr/bin/env python3
"""
scan_today_signals.py
---------------------
One-shot scan using UPSTOX (same data source as the landing page).
Fetches all TradeFinder Sector Scope stocks, runs the exact signal logic
from breakout_intraday_strategy.py, prints every signal with its timestamp.

Usage:
    cd /Users/Shared/Project/openalgo
    /Users/bond7/.local/bin/uv run python strategies/scan_today_signals.py
"""

import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta, datetime, timezone
from typing import Optional
import concurrent.futures

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except Exception:
    pass

import base64, hashlib, hmac, struct, requests, gzip
import numpy as np
import pandas as pd

try:
    import urllib3 as _u3; _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
TF_JWT_FILE = os.path.join(os.path.dirname(__file__), "tf_jwt.txt")
TF_JWT_TOKEN = os.getenv("TF_JWT_TOKEN", "")

_TF_BASE   = "https://tradefinder.in/api_be"
_TF_SECRET = "5ACHPKZUZNTWYPSJXNP7IULMACAM6P6Q"
_TF_SSL    = False

# Upstox — token read from landing page's SQLite
_LANDING_DB = "/Users/Shared/Project/indicator/landing-page/prisma/dev.db"
_INSTRUMENTS_CACHE = "/Users/Shared/Project/indicator/landing-page/.scanner-cache/upstox-nse-eq.json"
UPSTOX_V3 = "https://api.upstox.com/v3"
IST_OFFSET_SECONDS = 5.5 * 60 * 60   # 19800

# Signal config — aligned to the TradingView Pine indicator (Intraday_Signals_v5.6)
# published defaults so this scan matches the chart's alerts:
#   single AND double breakouts, VWAP filter ON, ADX OFF, candle High/Low SL, no min-SL gate.
USE_VWAP        = True    # useVWAPFilter=true
USE_ADX         = False   # useADXFilter=false
USE_ATR_SL      = True    # useATRSL=true → SL = Entry ± ATR×mult (chart setting)
ATR_LEN         = 14
ATR_MULT        = 1.5     # atrMult on the chart (reduces choppy whipsaw flips)
ADX_LEN         = 14
ADX_THRESHOLD   = 20.0
ENABLE_BUY      = True
ENABLE_SELL     = True
ENABLE_SINGLE   = True    # Pine buy1/sell1 are always on (single-candle breakouts)
ENABLE_DOUBLE   = True
MIN_SL_DIST_PCT = 0       # Pine has no minimum-SL gate

SESSION_START_MIN = 9 * 60 + 15
ENTRY_CUTOFF_MIN  = 15 * 60 + 20   # Pine session "0915-1525": last in-session 5m bar opens 15:20
SESSION_END_MIN   = 15 * 60 + 30


# ── Time helpers ──────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)

def _cur_min() -> int:
    n = _now_ist(); return n.hour * 60 + n.minute


# ── TradeFinder ───────────────────────────────────────────────────────────────

def _get_jwt():
    if os.path.exists(TF_JWT_FILE):
        t = open(TF_JWT_FILE).read().strip()
        if t: return t
    return TF_JWT_TOKEN

def _totp(ts_ms: int) -> str:
    raw = _TF_SECRET.upper()
    key = base64.b32decode(raw + "=" * ((8 - len(raw) % 8) % 8))
    ctr = int(ts_ms / 1000 / 30)
    h   = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    off = h[-1] & 0x0F
    return str((struct.unpack(">I", h[off:off+4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)

def _tf_ts() -> int:
    try:
        return int(requests.get(f"{_TF_BASE}/servertime", timeout=5, verify=_TF_SSL).json()["payload"]["data"])
    except Exception:
        return int(time.time() * 1000)

def _tf_headers():
    jwt = _get_jwt()
    if not jwt:
        raise RuntimeError("No TF JWT. Write to strategies/tf_jwt.txt")
    return {"jwttoken": jwt, "accesstoken": _totp(_tf_ts())}

def fetch_sector_scope() -> dict:
    resp = requests.get(f"{_TF_BASE}/data/sector_scope", headers=_tf_headers(),
                        timeout=15, verify=_TF_SSL).json()
    if resp.get("status") != "SUCCESS":
        raise RuntimeError(f"TF error: {resp.get('message')}")
    all_sector = ((resp.get("payload") or {}).get("data") or {}).get("all_sector") or {}
    result = {}
    for raw_key, stock_dict in all_sector.items():
        if not isinstance(stock_dict, dict): continue
        sector = raw_key.removesuffix("_r_factor").strip().upper()
        stocks = {}
        for sym, e in stock_dict.items():
            try:
                stocks[sym] = {"ltp": float(e["param_0"]),
                               "change_pct": float(e["param_2"]),
                               "r_factor": float(e["param_3"])}
            except Exception:
                continue
        if stocks:
            result[sector] = stocks
    return result


# ── Upstox data ───────────────────────────────────────────────────────────────

def _get_upstox_token() -> str:
    """Read Upstox access token from landing page's prisma/dev.db."""
    import sqlite3
    conn = sqlite3.connect(_LANDING_DB)
    row = conn.execute("SELECT upstoxAccessToken FROM Settings WHERE id=1").fetchone()
    conn.close()
    if not row or not row[0]:
        raise RuntimeError("No Upstox access token in landing page DB. Re-authorize first.")
    return row[0]

def _load_instrument_map() -> dict:
    """Load the symbol→instrumentKey map from landing page cache."""
    with open(_INSTRUMENTS_CACHE) as f:
        return json.load(f)

_instrument_map: Optional[dict] = None
def _resolve(symbol: str) -> Optional[str]:
    global _instrument_map
    if _instrument_map is None:
        _instrument_map = _load_instrument_map()
    return _instrument_map.get(symbol.upper().strip())

def _baked_sec(ts) -> int:
    """Upstox ts (ISO string or epoch ms) → IST-baked epoch seconds."""
    if isinstance(ts, (int, float)):
        real_ms = int(ts)
    else:
        real_ms = int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    return real_ms // 1000 + int(IST_OFFSET_SECONDS)

def fetch_candles_upstox(symbol: str, token: str, lookback_days: int = 5) -> Optional[pd.DataFrame]:
    key = _resolve(symbol)
    if not key:
        return None
    enc = requests.utils.quote(key, safe="")
    headers = {"accept": "application/json", "Authorization": f"Bearer {token}"}

    to_d   = date.today()
    from_d = to_d - timedelta(days=lookback_days)

    def _fetch(url):
        # Retry on Cloudflare 429 (error 1015) with exponential backoff so a
        # transient rate-limit doesn't silently turn into "no data" → 0 signals.
        for attempt in range(5):
            r = requests.get(f"{UPSTOX_V3}{url}", headers=headers, timeout=15)
            if r.status_code == 429:
                if attempt == 4:
                    raise RuntimeError("upstox_rate_limited_429")
                time.sleep(2 ** attempt)   # 1, 2, 4, 8s
                continue
            if not r.ok: return []
            d = r.json()
            if d.get("status") != "success": return []
            return d.get("data", {}).get("candles", [])
        return []

    intraday   = _fetch(f"/historical-candle/intraday/{enc}/minutes/5")
    historical = _fetch(f"/historical-candle/{enc}/minutes/5/{to_d}/{from_d}")

    by_time = {}
    for c in [*historical, *intraday]:
        t = _baked_sec(c[0])
        if not t: continue
        by_time[t] = {
            "timestamp": t,
            "open":   float(c[1]), "high":  float(c[2]),
            "low":    float(c[3]), "close": float(c[4]),
            "volume": float(c[5]) if len(c) > 5 else 0,
        }
    if not by_time:
        return None
    df = pd.DataFrame(sorted(by_time.values(), key=lambda x: x["timestamp"]))
    return df


# ── Indicators ────────────────────────────────────────────────────────────────

def _normalise_upstox(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Convert timestamp (IST-baked epoch seconds) to _dt/_date/_hm columns."""
    df = df.copy()
    # timestamp is IST-baked seconds → read as UTC to get IST wall clock
    df["_dt"]   = pd.to_datetime(df["timestamp"], unit="s", utc=False)
    df["_date"] = df["_dt"].dt.strftime("%Y-%m-%d")
    df["_hm"]   = df["_dt"].dt.hour * 60 + df["_dt"].dt.minute
    return df.sort_values("_dt").reset_index(drop=True)

def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    tp     = (df["high"] + df["low"] + df["close"]) / 3.0
    pv     = tp * df["volume"]
    cum_pv = pv.groupby(df["_date"]).cumsum()
    cum_v  = df["volume"].groupby(df["_date"]).cumsum().replace(0, np.nan)
    df["vwap"] = (cum_pv / cum_v).astype(float)
    return df

def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    h, l, c  = df["high"], df["low"], df["close"]
    prev_c   = c.shift(1)
    tr       = pd.concat([(h-l),(h-prev_c).abs(),(l-prev_c).abs()], axis=1).max(axis=1)
    up, dn   = h.diff(), -l.diff()
    plus_dm  = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    alpha    = 1.0 / period
    atr      = tr.astype(float).ewm(alpha=alpha, adjust=False).mean().replace(0, np.nan)
    plus_di  = 100 * plus_dm.astype(float).ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.astype(float).ewm(alpha=alpha, adjust=False).mean() / atr
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean().astype(float)
    return df

def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c  = c.shift(1)
    tr      = pd.concat([(h-l),(h-prev_c).abs(),(l-prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.astype(float).ewm(alpha=1.0/period, adjust=False).mean()
    return df


# ── Signal evaluation ─────────────────────────────────────────────────────────

def _eval_bar(df: pd.DataFrame, i: int) -> Optional[dict]:
    """Evaluate the Pine breakout on bar index i (using i-1, i-2 as the prior bars)."""
    b, p1, p2 = df.iloc[i], df.iloc[i-1], df.iloc[i-2]
    sig_hm = int(b["_hm"])
    if not (SESSION_START_MIN <= sig_hm <= ENTRY_CUTOFF_MIN): return None

    o, h, c, lo = float(b["open"]), float(b["high"]), float(b["close"]), float(b["low"])
    o1,h1,c1,l1 = float(p1["open"]),float(p1["high"]),float(p1["close"]),float(p1["low"])
    o2,h2,c2,l2 = float(p2["open"]),float(p2["high"]),float(p2["close"]),float(p2["low"])

    is_green  = c > o;   is_red   = c < o
    prev_green = c1 > o1; prev_red  = c1 < o1
    p2g = c2 > o2;        p2r = c2 < o2

    buy1  = ENABLE_SINGLE and ENABLE_BUY  and is_green and prev_red   and c > h1
    sell1 = ENABLE_SINGLE and ENABLE_SELL and is_red   and prev_green and c < l1
    buy2  = ENABLE_DOUBLE and ENABLE_BUY  and is_green and prev_green and p2r and c > h2
    sell2 = ENABLE_DOUBLE and ENABLE_SELL and is_red   and prev_red   and p2g and c < l2

    vwap = float(b["vwap"]) if "vwap" in b.index and pd.notna(b["vwap"]) else None
    adx  = float(b["adx"])  if "adx"  in b.index and pd.notna(b["adx"])  else 0.0
    atr  = float(b["atr"])  if USE_ATR_SL and "atr" in b.index and pd.notna(b["atr"]) else None

    vwap_buy_ok  = (not USE_VWAP) or (vwap is not None and c > vwap)
    vwap_sell_ok = (not USE_VWAP) or (vwap is not None and c < vwap)
    adx_ok       = (not USE_ADX)  or (adx >= ADX_THRESHOLD)

    def _sl_long(csl):
        return round(c - atr * ATR_MULT, 2) if (USE_ATR_SL and atr) else csl
    def _sl_short(csl):
        return round(c + atr * ATR_MULT, 2) if (USE_ATR_SL and atr) else csl

    if (buy1 or buy2) and vwap_buy_ok and adx_ok:
        csl    = min(lo, l1) if buy1 else min(lo, l1, l2)
        sl     = _sl_long(csl)
        sl_pct = abs(c - sl) / c * 100
        if MIN_SL_DIST_PCT > 0 and sl_pct < MIN_SL_DIST_PCT: return None
        if c <= sl: return None
        return {"direction": "LONG", "kind": "single" if buy1 else "double",
                "entry": round(c,2), "sl": sl, "sl_pct": round(sl_pct,2),
                "vwap": round(vwap,2) if vwap else 0, "adx": round(adx,1),
                "signal_time": b["_dt"].strftime("%H:%M"),
                "signal_ts":   b["_dt"].strftime("%Y-%m-%d %H:%M")}

    if (sell1 or sell2) and vwap_sell_ok and adx_ok:
        csl    = max(h, h1) if sell1 else max(h, h1, h2)
        sl     = _sl_short(csl)
        sl_pct = abs(c - sl) / c * 100
        if MIN_SL_DIST_PCT > 0 and sl_pct < MIN_SL_DIST_PCT: return None
        if c >= sl: return None
        return {"direction": "SHORT", "kind": "single" if sell1 else "double",
                "entry": round(c,2), "sl": sl, "sl_pct": round(sl_pct,2),
                "vwap": round(vwap,2) if vwap else 0, "adx": round(adx,1),
                "signal_time": b["_dt"].strftime("%H:%M"),
                "signal_ts":   b["_dt"].strftime("%Y-%m-%d %H:%M")}
    return None


def evaluate_signal(df_raw: pd.DataFrame, today: str) -> Optional[dict]:
    """
    Replay EVERY bar of today's session and return the FIRST valid breakout
    (matches the live strategy, which enters on the first signal while flat).
    The old version only checked the last closed bar, so morning signals
    (e.g. a 09:35 breakout) were silently missed.
    """
    df = _normalise_upstox(df_raw)
    if df is None or df.empty: return None

    # ATR warmed on ALL historical bars (cold-start fix — matches Python strategy)
    if USE_ATR_SL:
        df = add_atr(df, ATR_LEN)

    # Filter to today's session
    df = df[(df["_date"] == today) &
            (df["_hm"] >= SESSION_START_MIN) & (df["_hm"] <= SESSION_END_MIN)]
    if len(df) < 4: return None

    df = add_vwap(df)
    df = add_adx(df, ADX_LEN)
    df = df.reset_index(drop=True)

    # Drop the last bar only when the clock confirms it is still forming.
    last_hm = int(df.iloc[-1]["_hm"])
    if _cur_min() < last_hm + 5:
        df = df.iloc[:-1]
    if len(df) < 3: return None

    # Walk forward; first valid signal bar wins (one entry per stock per day).
    for i in range(2, len(df)):
        sig = _eval_bar(df, i)
        if sig:
            return sig
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def scan_one(args):
    symbol, today, token = args
    try:
        df = fetch_candles_upstox(symbol, token, lookback_days=5)
        if df is None: return symbol, None, "no_data_or_unresolved"
        sig = evaluate_signal(df, today)
        return symbol, sig, None
    except Exception as e:
        return symbol, None, str(e)


def main():
    print("Fetching TradeFinder Sector Scope...")
    sector_data = fetch_sector_scope()

    # Unique symbols with metadata
    all_symbols: dict[str, dict] = {}
    for sector, stocks in sector_data.items():
        for sym, info in stocks.items():
            if sym not in all_symbols:
                all_symbols[sym] = {"sector": sector, "r_factor": info["r_factor"],
                                    "change_pct": info["change_pct"]}

    today = _now_ist().strftime("%Y-%m-%d")
    print(f"Date (IST): {today}")
    print(f"Symbols   : {len(all_symbols)} unique across {len(sector_data)} sectors")
    print(f"Config    : USE_VWAP={USE_VWAP}  USE_ADX={USE_ADX}  USE_ATR_SL={USE_ATR_SL}  "
          f"ATR={ATR_LEN}×{ATR_MULT}  ENABLE_SINGLE={ENABLE_SINGLE}")
    print("-" * 110)

    print("Loading Upstox token...")
    token = _get_upstox_token()
    print(f"Upstox token: ...{token[-20:]}")

    sym_list = sorted(all_symbols.keys())
    WORKERS  = 4    # keep concurrency low — Upstox/Cloudflare rate-limits (429/1015) aggressively
    signals  = []
    errors   = []
    done     = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(scan_one, (sym, today, token)): sym for sym in sym_list}
        for fut in concurrent.futures.as_completed(futs):
            sym, sig, err = fut.result()
            done += 1
            if err and err != "no_data_or_unresolved":
                errors.append(f"{sym}: {err}")
            if sig:
                sig["symbol"] = sym
                sig["sector"] = all_symbols[sym]["sector"]
                sig["r_factor"] = all_symbols[sym]["r_factor"]
                signals.append(sig)
            print(f"  {done}/{len(sym_list)} | signals={len(signals)} | errors={len(errors)}   ", end="\r")

    print(f"\nDone: {done} scanned | {len(signals)} signals | {len(errors)} errors")

    if errors[:5]:
        print("Sample errors:", errors[:5])

    print()
    if not signals:
        print("No signals found today.")
        return

    signals.sort(key=lambda s: s["signal_time"])

    hdr = f"{'SYMBOL':<16} {'DIR':<6} {'KIND':<7} {'TIME':<6} {'ENTRY':>8} {'SL':>8} {'SL%':>5}  {'VWAP':>8} {'ADX':>5}  SECTOR"
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)
    for s in signals:
        print(f"{s['symbol']:<16} {s['direction']:<6} {s['kind']:<7} {s['signal_time']:<6} "
              f"{s['entry']:>8.2f} {s['sl']:>8.2f} {s['sl_pct']:>5.2f}%  "
              f"{s['vwap']:>8.2f} {s['adx']:>5.1f}  {s['sector']}")
    print(sep)
    print(f"Total: {len(signals)}  "
          f"LONG={sum(1 for s in signals if s['direction']=='LONG')}  "
          f"SHORT={sum(1 for s in signals if s['direction']=='SHORT')}")


if __name__ == "__main__":
    main()
