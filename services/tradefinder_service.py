# services/tradefinder_service.py
"""
TradeFinder market_pulse client — read-only HTTP client for the three ranked
lists (intraday_boost, breakout_beacon, high_powered_stocks). Ported from
strategies/isi_v56_live_strategy.py's TF auth/fetch logic, keeping all four
per-item fields instead of discarding everything but the score.

Auth: jwttoken (user-pasted, file-backed) + accesstoken (TOTP). Same token
file the ISI strategies already use — get the token: tradefinder.in ->
DevTools -> Application -> Local Storage -> 'lt', then:
echo '<token>' > strategies/tf_jwt.txt
"""

from __future__ import annotations
import base64
import hashlib
import hmac
import os
import struct
import time
from typing import Optional

import requests

from utils.logging import get_logger

logger = get_logger(__name__)

TF_BASE = os.getenv("TF_BASE", "https://tradefinder.in/api_be")
TF_SECRET = os.getenv("TF_SECRET", "5ACHPKZUZNTWYPSJXNP7IULMACAM6P6Q")
TF_JWT_TOKEN = os.getenv("TF_JWT_TOKEN", "")

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TF_JWT_FILE = os.getenv("TF_JWT_FILE", os.path.join(_project_root, "strategies", "tf_jwt.txt"))

# Response keys can vary; fall back through known alternates (matches the
# openalgo-chart TS client's mapRawItems()/fetchMarketPulse() behavior).
_LIST_KEYS = {
    "intraday_boost": ("intraday_boost",),
    "breakout_beacon": ("breakout_beacon", "beacon", "breakout"),
    "high_powered_stocks": ("high_powered_stocks", "high_powered", "powered"),
}


def _get_tf_jwt() -> str:
    """Read JWT fresh on every call (file first, then env fallback) so a manual
    refresh takes effect on the very next tick — no cache, no restart needed."""
    if TF_JWT_FILE and os.path.exists(TF_JWT_FILE):
        try:
            token = open(TF_JWT_FILE).read().strip()
            if token:
                return token
        except Exception:
            pass
    return TF_JWT_TOKEN


def _tf_totp(ts_ms: int, step: int = 30) -> str:
    """RFC 6238 TOTP — HMAC-SHA1, 30s window, 6 digits."""
    counter = int(ts_ms / 1000 / step)
    key = base64.b32decode(TF_SECRET.upper() + "=" * ((8 - len(TF_SECRET) % 8) % 8))
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def _tf_server_time_ms() -> int:
    try:
        r = requests.get(f"{TF_BASE}/servertime", timeout=5)
        return int(r.json()["payload"]["data"])
    except Exception:
        return int(time.time() * 1000)


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _map_items(raw: list[dict]) -> list[dict]:
    """Symbol->symbol, param_0->ltp, param_1->prev_close, param_2->change_pct,
    param_3->score. Sorted by |score| desc (same convention as the strategy
    and the openalgo-chart TS client's mapRawItems()).

    breakout_beacon uses a different schema per-field (param_2 is a "BULL"/
    "BEAR" sentiment label, not a numeric change_pct; param_0/param_1 aren't
    genuine ltp/prev_close either). Each field is converted independently
    (non-numeric -> 0.0) rather than in one all-or-nothing try/except, so one
    off-schema field (e.g. "BEAR") no longer drops the whole item — matching
    the TS client, which never dropped these rows to begin with."""
    items = []
    for it in raw:
        symbol = it.get("Symbol")
        if not symbol:
            continue
        items.append({
            "symbol": symbol,
            "ltp": _safe_float(it.get("param_0")),
            "prev_close": _safe_float(it.get("param_1")),
            "change_pct": _safe_float(it.get("param_2")),
            "score": _safe_float(it.get("param_3")),
        })
    items.sort(key=lambda x: abs(x["score"]), reverse=True)
    return items


def fetch_market_pulse() -> Optional[dict[str, list[dict]]]:
    """Fetch all three TradeFinder ranked lists in one call. Returns None on
    ANY failure (empty/expired JWT, network error, TF error payload) so the
    caller can skip this tick instead of writing partial/garbage data."""
    jwt = _get_tf_jwt()
    if not jwt:
        logger.warning(f"TF_JWT_TOKEN is empty. Quick fix: echo '<token>' > {TF_JWT_FILE}")
        return None
    totp = _tf_totp(_tf_server_time_ms())
    try:
        r = requests.get(f"{TF_BASE}/data/market_pulse",
                          headers={"jwttoken": jwt, "accesstoken": totp}, timeout=10)
        data = r.json()
    except Exception as e:
        logger.warning(f"TradeFinder market_pulse request failed: {e}")
        return None
    if data.get("status") == "ERROR":
        logger.warning(f"TradeFinder error: {data.get('code')} — {data.get('message')} "
                        f"(JWT expired? paste a fresh token into {TF_JWT_FILE})")
        return None
    payload_data = (data.get("payload") or {}).get("data") or {}
    result = {}
    for list_type, alt_keys in _LIST_KEYS.items():
        raw = []
        for key in alt_keys:
            raw = payload_data.get(key)
            if raw:
                break
        result[list_type] = _map_items(raw or [])
    return result


def fetch_sector_scope() -> Optional[dict]:
    """Fetch TradeFinder's sector rfactor index + per-sector stock breakdown
    (the two calls openalgo-chart's SectorScope.tsx makes client-side) using
    the server's own auto-refreshing JWT. Returns None on ANY failure (empty/
    expired JWT, network error, TF error payload) — mirrors fetch_market_pulse's
    all-or-nothing contract so the caller can fall back cleanly."""
    jwt = _get_tf_jwt()
    if not jwt:
        logger.warning(f"TF_JWT_TOKEN is empty. Quick fix: echo '<token>' > {TF_JWT_FILE}")
        return None
    totp = _tf_totp(_tf_server_time_ms())
    headers = {"jwttoken": jwt, "accesstoken": totp}
    try:
        r_index = requests.get(f"{TF_BASE}/data/order/daily-index", headers=headers, timeout=10)
        r_sectors = requests.get(f"{TF_BASE}/data/order/all_sector", headers=headers, timeout=10)
        index_data = r_index.json()
        sectors_data = r_sectors.json()
    except Exception as e:
        logger.warning(f"TradeFinder sector scope request failed: {e}")
        return None
    if index_data.get("status") == "ERROR" or sectors_data.get("status") == "ERROR":
        logger.warning(f"TradeFinder sector scope error (JWT expired? paste a fresh token into {TF_JWT_FILE})")
        return None
    return {
        "index": (index_data.get("payload") or {}).get("data") or [],
        "sectors": (sectors_data.get("payload") or {}).get("data") or {},
    }
