# broker/dhan/api/expired_data.py
"""
Dhan Expired Options Data API Client

Provides synchronous access to Dhan's /v2/charts/rollingoption endpoint for
fetching historical minute-level data of *expired* option contracts.

This is the Dhan counterpart to broker/upstox/api/expired_data.py, but Dhan's
model is fundamentally different from Upstox's expired-instruments API, so the
two clients do NOT share an interface 1:1. Key differences the service layer
must account for:

  1. OPTIONS ONLY. Dhan's rolling-option endpoint does not serve expired
     FUTURES. There is no Dhan equivalent of Upstox's /future/contract.

  2. NO PER-CONTRACT ENUMERATION. Upstox lists every expired instrument_key for
     an expiry; Dhan instead asks you to specify an ATM-relative strike offset
     ("ATM", "ATM+10" ... "ATM-10" for index options; "ATM+3" ... "ATM-3" for
     stock options) plus an option type (CALL/PUT). The concrete strike price
     is only known *after* the call — it comes back in the response `strike[]`
     array. So OpenAlgo symbols can only be finalised post-fetch.

  3. NO EXPIRY-DATE LIST ON THIS ENDPOINT. Upstox returns YYYY-MM-DD expiry
     dates; Dhan addresses an expiry by `expiryFlag` (WEEK | MONTH) +
     `expiryCode` (integer: 0 = nearest expired, 1 = next, ...). To map those
     back to human dates you must cross-reference Dhan's option-chain expiry
     list separately (see TODO in list_expiries).

  4. COLUMN-ORIENTED RESPONSE. Dhan returns parallel arrays
     (data.ce.open[], data.ce.timestamp[], ...) rather than row candles.
     normalize_candles() converts this into the row format the service's
     _candles_to_df() already expects: [[iso_ts, o, h, l, c, v, oi], ...].

Requires Dhan's Data API subscription to be active on the account (the same
subscription used by broker/dhan/api/data.py — error codes 806/820/821 signal
a missing subscription).

NOTE: Fields marked "VERIFY" below could not be confirmed against a live API
response and Dhan's public Annexure; confirm them against a real account before
relying on this in production.
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Any

from broker.dhan.api.baseurl import get_url
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# Dhan expired-options endpoint (POST). Lives under the same api.dhan.co host
# as the rest of broker/dhan/api/data.py.
ROLLING_OPTION_ENDPOINT = "/v2/charts/rollingoption"

# IST offset applied when converting Dhan epoch timestamps to the ISO strings
# the service's _candles_to_df() consumes.
_IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60  # 19800

# Dhan serves at most 30 days of candles per rolling-option call.
MAX_DAYS_PER_CALL = 30

# How many past expiry "slots" Phase 1 exposes per flag. Dhan addresses expiries
# by integer code (0 = nearest past expiry), so these are simply codes 0..N-1.
DEFAULT_WEEKLY_SLOTS = 8
DEFAULT_MONTHLY_SLOTS = 6


# =============================================================================
# Underlying resolution
# =============================================================================
#
# Dhan addresses the underlying by (securityId, exchangeSegment, instrument).
# The primary path resolves securityId dynamically from OpenAlgo's master
# contract DB (which already holds Dhan security IDs), so we avoid hardcoding.
# The fallback map below carries the well-known index security IDs for when a
# dynamic lookup is unavailable.
#
# TODO(VERIFY): confirm these index security IDs against Dhan's instrument
# master (api.dhan.co scrip master CSV). They are the commonly cited values but
# have not been verified here.
_INDEX_FALLBACK: dict[str, dict[str, Any]] = {
    "NIFTY":      {"security_id": 13,  "exchange_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "BANKNIFTY":  {"security_id": 25,  "exchange_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "FINNIFTY":   {"security_id": 27,  "exchange_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "MIDCPNIFTY": {"security_id": 442, "exchange_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "NIFTYNXT50": {"security_id": 38,  "exchange_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "SENSEX":     {"security_id": 51,  "exchange_segment": "BSE_FNO", "instrument": "OPTIDX"},
    "BANKEX":     {"security_id": 69,  "exchange_segment": "BSE_FNO", "instrument": "OPTIDX"},
}

# Underlyings exposed in the UI. Matches the Upstox SUPPORTED_UNDERLYINGS set so
# the frontend dropdown stays consistent regardless of active broker.
SUPPORTED_UNDERLYINGS = list(_INDEX_FALLBACK.keys())

# OpenAlgo exchange tag (NFO/BFO) per underlying — used for symbol storage.
UNDERLYING_EXCHANGE: dict[str, str] = {
    "NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO", "NIFTYNXT50": "NFO",
    "SENSEX": "BFO", "BANKEX": "BFO",
}


def resolve_underlying(underlying: str) -> dict[str, Any] | None:
    """Resolve an OpenAlgo underlying to Dhan's (security_id, exchange_segment, instrument).

    Tries the master-contract DB first (covers stock options too), then falls
    back to the hardcoded index map.

    Returns:
        dict with keys ``security_id``, ``exchange_segment``, ``instrument``,
        ``exchange`` (OpenAlgo NFO/BFO tag), or None if unresolvable.
    """
    underlying = underlying.upper()

    # Dynamic lookup from OpenAlgo's master contracts (holds Dhan security IDs).
    try:
        from database.token_db import get_token

        for oa_exchange, segment, instrument in (
            ("NSE_INDEX", "NSE_FNO", "OPTIDX"),
            ("BSE_INDEX", "BSE_FNO", "OPTIDX"),
            ("NSE", "NSE_FNO", "OPTSTK"),
        ):
            token = get_token(underlying, oa_exchange)
            if token:
                return {
                    "security_id": int(token),
                    "exchange_segment": segment,
                    "instrument": instrument,
                    "exchange": "BFO" if segment == "BSE_FNO" else "NFO",
                }
    except Exception as e:
        logger.debug(f"Dynamic security-id lookup failed for {underlying}: {e}")

    # Fallback to the well-known index map.
    if underlying in _INDEX_FALLBACK:
        cfg = dict(_INDEX_FALLBACK[underlying])
        cfg["exchange"] = UNDERLYING_EXCHANGE.get(underlying, "NFO")
        return cfg

    return None


def strike_offsets(instrument: str) -> list[str]:
    """Return the ATM-relative strike offsets Dhan serves for a contract type.

    Index options: ATM-10 .. ATM+10. Other (stock) options: ATM-3 .. ATM+3.
    """
    span = 10 if instrument == "OPTIDX" else 3
    offsets = ["ATM"]
    for i in range(1, span + 1):
        offsets.append(f"ATM+{i}")
        offsets.append(f"ATM-{i}")
    return offsets


# =============================================================================
# Rate limiter — Dhan data APIs are ~1 request/second (mirrors data.py:805 path)
# =============================================================================


class _DhanRateLimiter:
    """Serialises calls to a safe rate for Dhan's data API.

    Dhan's data API permits ~5 req/s and 100k req/day (per the published
    limits). Default min_interval=0.2s ⇒ ~5 req/s.
    """

    def __init__(self, min_interval: float = 0.2) -> None:
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        sleep_for = 0.0
        with self._lock:
            now = time.monotonic()
            gap = now - self._last
            if gap < self._min_interval:
                sleep_for = self._min_interval - gap
            # Reserve the slot immediately so concurrent callers stagger.
            self._last = now + sleep_for
        if sleep_for > 0:
            time.sleep(sleep_for)


_rate_limiter = _DhanRateLimiter()


# =============================================================================
# Client
# =============================================================================


class DhanExpiredDataClient:
    """Synchronous client for Dhan's expired-options rolling endpoint.

    Args:
        auth_token: Valid Dhan access-token from OpenAlgo's auth DB.
        client_id:  Dhan client id (the part before ``:::`` in BROKER_API_KEY).
                    If omitted, it is read from the BROKER_API_KEY env var.
    """

    def __init__(self, auth_token: str, client_id: str | None = None) -> None:
        self.auth_token = auth_token
        self.client_id = client_id or self._client_id_from_env()
        self._client = get_httpx_client()

    @staticmethod
    def _client_id_from_env() -> str:
        import os

        broker_api_key = os.getenv("BROKER_API_KEY")
        if not broker_api_key:
            raise ValueError("BROKER_API_KEY not set; cannot derive Dhan client_id")
        return broker_api_key.split(":::")[0] if ":::" in broker_api_key else broker_api_key

    def _headers(self) -> dict[str, str]:
        return {
            "access-token": self.auth_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def list_expiries(self, security_id: int, exchange_segment: str) -> list[str]:
        """Return available expiry dates for the underlying.

        TODO(integration): Dhan's rolling-option endpoint does NOT expose an
        expiry list — it takes an integer ``expiryCode`` + ``expiryFlag``
        instead. To surface human-readable YYYY-MM-DD expiries (as the Upstox
        pipeline expects), wire Dhan's option-chain expiry-list endpoint
        (``/v2/optionchain/expirylist``) here and translate to expiryCode by
        ordering past expiries newest-first. Returning [] for now keeps the
        scaffold honest rather than fabricating dates.
        """
        logger.warning(
            "DhanExpiredDataClient.list_expiries is not implemented — Dhan uses "
            "expiryCode/expiryFlag, not an expiry-date list. See TODO."
        )
        return []

    def get_expired_option_data(
        self,
        *,
        security_id: int,
        exchange_segment: str,
        instrument: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        option_type: str,
        from_date: str,
        to_date: str,
        interval: str = "1",
        required_data: list[str] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any] | None:
        """Fetch expired-option candles for one ATM-relative strike.

        Args:
            security_id:     Underlying security id (index/stock), not the option's.
            exchange_segment: Derivatives segment, e.g. "NSE_FNO" / "BSE_FNO".
            instrument:      "OPTIDX" (index options) or "OPTSTK" (stock options).
            expiry_flag:     "WEEK" or "MONTH".
            expiry_code:     Integer expiry selector (0 = nearest expired, 1 = next, ...).
            strike:          ATM-relative offset, e.g. "ATM", "ATM+1", "ATM-10".
            option_type:     "CALL" or "PUT".
            from_date/to_date: "YYYY-MM-DD" (Dhan serves up to 30 days per call).
            interval:        Minute interval: "1", "5", "15", "25", "60".
            required_data:   Fields to request; defaults to OHLCV + OI.

        Returns:
            Parsed JSON dict on success (HTTP 200), or None on failure after
            retries (caller should treat None as retryable, mirroring the
            Upstox client's contract).
        """
        if required_data is None:
            # Mirror the ExpiryFlow reference: request strike/iv/spot too so the
            # per-bar strike is available for OpenAlgo symbol generation.
            required_data = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]

        body = {
            "exchangeSegment": exchange_segment,
            "interval": interval,
            "securityId": security_id,
            "instrument": instrument,
            "expiryFlag": expiry_flag,
            "expiryCode": expiry_code,
            "strike": strike,
            "drvOptionType": option_type.upper(),
            "requiredData": required_data,
            "fromDate": from_date,
            "toDate": to_date,
        }

        url = get_url(ROLLING_OPTION_ENDPOINT)
        import json as _json

        payload = _json.dumps(body)

        for attempt in range(max_retries):
            _rate_limiter.acquire()
            try:
                resp = self._client.post(url, headers=self._headers(), content=payload)
            except Exception as e:
                wait = 2**attempt
                logger.warning(
                    f"Dhan rolling-option request failed (attempt {attempt + 1}): {e}; "
                    f"retry in {wait}s"
                )
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                data = resp.json()
                # Dhan signals app-level failure with status="failed" even on 200.
                if isinstance(data, dict) and data.get("status") == "failed":
                    err = data.get("data", {})
                    code = next(iter(err), "unknown") if isinstance(err, dict) else "unknown"
                    # 805 = rate limit; back off and retry.
                    if code == "805":
                        wait = 2.0 * (2**attempt)
                        logger.warning(f"Dhan rate limit (805); retry in {wait}s")
                        time.sleep(wait)
                        continue
                    logger.error(f"Dhan rolling-option app error {code}: {err}")
                    return None
                return data

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2**attempt
                logger.warning(
                    f"Dhan HTTP {resp.status_code} on rolling-option, "
                    f"retry {attempt + 1}/{max_retries} in {wait}s"
                )
                time.sleep(wait)
                continue

            logger.error(
                f"Dhan rolling-option HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return None

        logger.error(f"All {max_retries} retries exhausted for Dhan rolling-option")
        return None


# =============================================================================
# Response normalisation
# =============================================================================


def normalize_candles(
    response: dict[str, Any], option_type: str
) -> tuple[list[list], float | None]:
    """Convert Dhan's column-oriented response to the service's row format.

    The service's _candles_to_df() expects rows shaped like Upstox's:
        [iso_timestamp, open, high, low, close, volume, oi]

    Dhan returns parallel arrays under data.ce / data.pe. This pivots them to
    rows and converts the epoch timestamp to an IST ISO-8601 string.

    Args:
        response:    Parsed JSON from get_expired_option_data().
        option_type: "CALL"/"CE" -> reads data.ce; "PUT"/"PE" -> reads data.pe.

    Returns:
        (rows, resolved_strike) where resolved_strike is the concrete strike
        price Dhan resolved the ATM offset to (from strike[], if present), or
        None. Returns ([], None) when there is no data.
    """
    if not response:
        return [], None

    side = "ce" if option_type.upper() in ("CALL", "CE") else "pe"
    block = (response.get("data") or {}).get(side)
    if not block:
        return [], None

    ts = block.get("timestamp") or []
    opens = block.get("open") or []
    highs = block.get("high") or []
    lows = block.get("low") or []
    closes = block.get("close") or []
    vols = block.get("volume") or []
    ois = block.get("oi") or []
    strikes = block.get("strike") or []

    def _at(arr: list, i: int) -> Any:
        return arr[i] if i < len(arr) else 0

    rows: list[list] = []
    for i, epoch in enumerate(ts):
        try:
            iso = datetime.utcfromtimestamp(int(epoch) + _IST_OFFSET_SECONDS).isoformat()
        except (ValueError, TypeError, OSError):
            continue
        rows.append(
            [
                iso,
                _at(opens, i),
                _at(highs, i),
                _at(lows, i),
                _at(closes, i),
                _at(vols, i),
                _at(ois, i),
            ]
        )

    # For a single (flag, code, offset, type) the strike is constant; take the
    # first non-null value as the contract's resolved strike.
    resolved_strike = None
    for s in strikes:
        if s is not None:
            try:
                resolved_strike = float(s)
                break
            except (ValueError, TypeError):
                continue
    return rows, resolved_strike


def normalize_rolling(response: dict[str, Any], option_type: str) -> list[dict[str, Any]]:
    """Pivot a rolling-option response into per-bar dicts (relative-coords model).

    Keeps every field Dhan returns, including the drifting per-bar ``strike_price``
    and ``spot`` — this is the honest representation of the continuous rolling
    series (no fixed-strike reconstruction). Timestamps are shifted to IST epoch.

    Returns a list of dicts with: timestamp (IST epoch int), open, high, low,
    close, volume, oi, iv, spot, strike_price.
    """
    if not response:
        return []
    side = "ce" if option_type.upper() in ("CALL", "CE") else "pe"
    block = (response.get("data") or {}).get(side)
    if not block:
        return []

    ts = block.get("timestamp") or []
    opens = block.get("open") or []
    highs = block.get("high") or []
    lows = block.get("low") or []
    closes = block.get("close") or []
    vols = block.get("volume") or []
    ois = block.get("oi") or []
    ivs = block.get("iv") or []
    spots = block.get("spot") or []
    strikes = block.get("strike") or []

    def _at(arr: list, i: int) -> Any:
        return arr[i] if i < len(arr) else None

    out: list[dict[str, Any]] = []
    for i, epoch in enumerate(ts):
        try:
            ist_epoch = int(epoch) + _IST_OFFSET_SECONDS
        except (ValueError, TypeError):
            continue
        out.append(
            {
                "timestamp": ist_epoch,
                "open": _at(opens, i),
                "high": _at(highs, i),
                "low": _at(lows, i),
                "close": _at(closes, i),
                "volume": _at(vols, i),
                "oi": _at(ois, i),
                "iv": _at(ivs, i),
                "spot": _at(spots, i),
                "strike_price": _at(strikes, i),
            }
        )
    return out


# =============================================================================
# Synthetic key encoding + expiry slots
# =============================================================================
#
# The OpenAlgo pipeline keys contracts by a single string (expired_instrument_key)
# and partitions by an underlying key (the legacy `upstox_key` column). Dhan has
# no per-contract instrument id, so we synthesise both from the rolling-option
# coordinates. Everything the download worker needs to re-issue the call is
# packed into the key.
#
#   underlying key   : "DHAN:{security_id}:{exchange_segment}"
#   contract key     : "DHAN|{sid}|{seg}|{instr}|{flag}|{code}|{offset}|{opt}"
#
# (Different separators so the underlying key never collides with a contract key.)

_KEY_PREFIX = "DHAN"
_KEY_SEP = "|"


def underlying_key(security_id: int, exchange_segment: str) -> str:
    """Build the partition key stored in the contracts/expiries `upstox_key` column."""
    return f"{_KEY_PREFIX}:{security_id}:{exchange_segment}"


def encode_contract_key(
    security_id: int,
    exchange_segment: str,
    instrument: str,
    expiry_flag: str,
    expiry_code: int,
    strike_offset: str,
    option_type: str,
) -> str:
    """Pack the rolling-option coordinates into the contract primary key."""
    return _KEY_SEP.join(
        [
            _KEY_PREFIX,
            str(security_id),
            exchange_segment,
            instrument,
            expiry_flag,
            str(expiry_code),
            strike_offset,
            option_type.upper(),
        ]
    )


def decode_contract_key(key: str) -> dict[str, Any] | None:
    """Decode a contract key back to rolling-option call parameters.

    Returns None if the key is not a Dhan contract key (lets the worker fall
    through to the Upstox path).
    """
    if not key or not key.startswith(_KEY_PREFIX + _KEY_SEP):
        return None
    parts = key.split(_KEY_SEP)
    if len(parts) != 8:
        return None
    _, sid, seg, instr, flag, code, offset, opt = parts
    try:
        return {
            "security_id": int(sid),
            "exchange_segment": seg,
            "instrument": instr,
            "expiry_flag": flag,
            "expiry_code": int(code),
            "strike_offset": offset,
            "option_type": opt,
        }
    except ValueError:
        return None


def is_dhan_key(key: str) -> bool:
    """True if ``key`` is a Dhan synthetic contract key."""
    return bool(key) and key.startswith(_KEY_PREFIX + _KEY_SEP)


def _last_weekday_on_or_before(d: datetime, weekday: int) -> datetime:
    """Return the most recent date <= d falling on the given weekday (Mon=0)."""
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def approx_expiry_date(expiry_flag: str, expiry_code: int, ref: datetime | None = None) -> str:
    """Approximate the real expiry date for a (flag, code) slot, as YYYY-MM-DD.

    This is ONLY an anchor for the download window and UI ordering — the true
    expiry date is inferred from the downloaded data (max candle timestamp).
    Uses Thursday as the canonical expiry weekday; holiday/rule shifts are
    absorbed by querying a generous window around the anchor.
    """
    ref = ref or datetime.utcnow()
    if expiry_flag.upper() == "WEEK":
        last_thu = _last_weekday_on_or_before(ref, 3)  # Thursday
        anchor = last_thu - timedelta(weeks=expiry_code)
    else:  # MONTH: last Thursday of (this month - code)
        month = ref.month - expiry_code
        year = ref.year
        while month <= 0:
            month += 12
            year -= 1
        # Last day of that month, then walk back to Thursday.
        if month == 12:
            first_next = datetime(year + 1, 1, 1)
        else:
            first_next = datetime(year, month + 1, 1)
        last_day = first_next - timedelta(days=1)
        anchor = _last_weekday_on_or_before(last_day, 3)
    return anchor.strftime("%Y-%m-%d")


# NSE/BSE full-day trading holidays that fall on a would-be monthly-expiry
# weekday (Thursday). When a month's last Thursday is a holiday, the monthly
# expiry rolls back to the previous trading day. This list only needs the
# holidays that can affect a *last-Thursday* of a month; extend as needed.
# The harvester also self-checks against the data's last candle, so an
# incomplete list degrades to a skipped month, not corrupt data.
_EXPIRY_HOLIDAYS: set[str] = {
    "2021-11-04",  # Diwali (Thu) — Nov monthly rolled
    "2022-04-14",  # Ambedkar Jayanti / Mahavir (Thu)
    "2023-03-30",  # Ram Navami (Thu)
    "2024-03-25",  # Holi (Mon, not Thu) — placeholder, harmless
    "2024-08-15",  # Independence Day (Thu) — Aug last Thu unaffected; harmless
    "2025-02-26",  # Mahashivratri
}


def _is_holiday(d: datetime) -> bool:
    return d.strftime("%Y-%m-%d") in _EXPIRY_HOLIDAYS


def monthly_expiries(years: int = 5, ref: datetime | None = None) -> list[str]:
    """Return monthly expiry dates (YYYY-MM-DD) for the past ``years``, newest first.

    Each is the last Thursday of the month, rolled back one trading day if that
    Thursday is a known holiday. Only months strictly before ``ref`` are returned
    (we harvest *expired* contracts). The harvester confirms each against the
    downloaded data's last candle, so minor calendar imperfections self-correct
    or are skipped rather than mislabeled.
    """
    ref = ref or datetime.utcnow()
    out: list[str] = []
    # Walk back month by month from the current month.
    y, m = ref.year, ref.month
    for _ in range(years * 12 + 1):
        # Last Thursday of (y, m)
        first_next = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
        last_day = first_next - timedelta(days=1)
        exp = _last_weekday_on_or_before(last_day, 3)  # Thursday
        while _is_holiday(exp):
            exp -= timedelta(days=1)
        # Only include expiries strictly in the past.
        if exp < ref:
            out.append(exp.strftime("%Y-%m-%d"))
        # Step to previous month
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def generate_expiry_slots(
    weekly_slots: int = DEFAULT_WEEKLY_SLOTS,
    monthly_slots: int = DEFAULT_MONTHLY_SLOTS,
    include_weekly: bool = True,
    ref: datetime | None = None,
) -> list[dict[str, Any]]:
    """Enumerate the past-expiry slots Phase 1 exposes.

    Returns a list of dicts: {expiry_flag, expiry_code, anchor_date, is_weekly}.
    """
    slots: list[dict[str, Any]] = []
    if include_weekly:
        for code in range(weekly_slots):
            slots.append(
                {
                    "expiry_flag": "WEEK",
                    "expiry_code": code,
                    "anchor_date": approx_expiry_date("WEEK", code, ref),
                    "is_weekly": True,
                }
            )
    for code in range(monthly_slots):
        slots.append(
            {
                "expiry_flag": "MONTH",
                "expiry_code": code,
                "anchor_date": approx_expiry_date("MONTH", code, ref),
                "is_weekly": False,
            }
        )
    return slots


def chunk_date_range(from_date: str, to_date: str) -> list[tuple[str, str]]:
    """Split [from_date, to_date] into <=MAX_DAYS_PER_CALL windows (YYYY-MM-DD).

    Mirrors ExpiryFlow's _chunk_date_range — Dhan serves at most 30 days/call.
    """
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=MAX_DAYS_PER_CALL), end)
        chunks.append((cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cursor = chunk_end + timedelta(days=1)
    return chunks
