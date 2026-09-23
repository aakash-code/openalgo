# services/tf_future_liquidity_service.py
"""
Current-month future tradeability for the TradeFinder Intraday Boost list.

An option tracks its underlying future, so the future's book decides whether a
position can be got out of. A stock whose future is quoted 0.01% wide and one
quoted 0.17% wide look identical on the ranked list, and the difference is paid
on every entry and every exit.

**Why this reads the live book rather than the previous session's bhavcopy.**
The F&O bhavcopy is already downloaded daily (services/nse_oi_bhavcopy.py) and
carries TtlTrfVal per contract, which would make a turnover tier free. Measured
on 22-Sep-2026 against live quotes for 19 contracts, previous-day turnover rank
correlates with live spread rank at Spearman **0.46** -- too weak to use. The
disagreements are not at the margin:

    BAJAJHLDNG  turnover #18 (worst)  but spread #4  (tight)
    INOXWIND    turnover #17          but spread #5
    SOLARINDS   turnover #6           but spread #15  (0.099%, Rs 20/unit)
    OIL         turnover #10          but spread #18  (widest)

A turnover tier would have rated SOLARINDS and OIL as fine on the day SOLARINDS
ran from rank 79 to 1. So the cheap proxy is deliberately not used.

Same non-blocking ensure/attach contract as tf_cpr_service.py and
tf_directional_score_service.py. Unlike CPR, spread moves through the session,
so entries go stale after REFRESH_INTERVAL_SEC. Unlike all three siblings, the
whole list costs ONE broker call rather than one per symbol -- the multiquote
path batches 500 at a time -- so the background thread is about not blocking the
request, not about rate limits.

A symbol with no cached entry gets None, never a zero: "no data yet" and "quoted
at zero spread" must not render the same.
"""

from __future__ import annotations

import re
import statistics
import threading
import time
from collections import deque
from datetime import date, datetime

from database.symbol import SymToken, db_session
from services.quotes_service import get_multiquotes_with_auth
from services.tf_symbol_alias import tradable_symbol
from utils.logging import get_logger

logger = get_logger(__name__)

REFRESH_INTERVAL_SEC = 30  # one batched call; the window below needs the samples

# The reported spread is a rolling MEDIAN of the last SAMPLE_WINDOW readings,
# not the latest one.
#
# An instantaneous spread is far too noisy to act on. Measured 22-Sep-2026,
# 12 contracts sampled every 12s for three minutes: CONCOR read 0.096, then
# 0.043, then 0.075; TRENT went 0.032 -> 0.093. Counting how often a contract
# crossed a tier boundary over those samples:
#
#     raw reading      57 tier flips
#     median of 3      37
#     median of 5      15      <- 74% fewer than raw
#
# A chip that says WIDE one minute and tight the next teaches you to ignore it,
# and worse, can report a healthy book at the exact moment you look. The median
# answers "how wide has this been", which is the question that matters before
# taking a position. At a 30s refresh, five samples is about 2.5 minutes.
SAMPLE_WINDOW = 5

# Spread tiers, as a fraction of price. Starting points from a 19-contract
# sample spanning TITAN (0.010%) to IEX (0.168%); expect to tune these after a
# few sessions rather than treating them as settled.
SPREAD_TIGHT_PCT = 0.03
SPREAD_WIDE_PCT = 0.08

# symbol -> (date_str, computed_at_monotonic, fut_symbol, spread_pct, spread_rs, turnover_cr)
_cache: dict[str, tuple[str, float, str, float | None, float | None, float | None]] = {}
# symbol -> recent instantaneous spread_pct readings, newest last.
_samples: dict[str, deque[float]] = {}
_pending: set[str] = set()
_lock = threading.Lock()

# underlying -> current-month future symbol, rebuilt once per day.
_futures: dict[str, str] = {}
_futures_day: str = ""
_futures_lock = threading.Lock()

# SYMBOL + DDMMMYY + FUT, e.g. RELIANCE29SEP26FUT. The underlying may carry
# '&' or '-' (M&M, BAJAJ-AUTO), so it is matched lazily rather than as [A-Z]+.
_FUT_RE = re.compile(r"^(.+?)(\d{2}[A-Z]{3}\d{2})FUT$")


def _parse_expiry(value: str) -> datetime:
    """'29-SEP-26' or '29-SEP-2026' -> datetime; unparseable sorts last so a
    bad row can never win the "nearest expiry" race."""
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.max


def _load_current_month_futures() -> dict[str, str]:
    """Every underlying's nearest non-expired future, in ONE query.

    strategy_module.symbol_resolver.resolve_expiry_rank answers this properly
    but runs a SymToken scan per call, and this list is ~200 symbols on every
    poll. One scan for the whole master is cheaper by two orders of magnitude.

    The instrument type is the trap: the active broker normalises stock and
    index futures to plain **'FUT'**, so filtering on FUTSTK/FUTIDX alone
    returns zero rows and every chip silently reads "no future". The same three
    names are matched in services/expiry_service.py:126.
    """
    rows = (
        db_session.query(SymToken.symbol, SymToken.expiry)
        .filter(
            SymToken.exchange == "NFO",
            SymToken.instrumenttype.in_(["FUT", "FUTSTK", "FUTIDX"]),
            SymToken.expiry.isnot(None),
            SymToken.expiry != "",
        )
        .all()
    )

    today = datetime.now()
    best: dict[str, tuple[datetime, str]] = {}
    for symbol, expiry in rows:
        matched = _FUT_RE.match(symbol or "")
        if not matched:
            continue
        when = _parse_expiry(expiry)
        if when < today or when == datetime.max:
            continue
        underlying = matched.group(1)
        if underlying not in best or when < best[underlying][0]:
            best[underlying] = (when, symbol)
    return {u: sym for u, (_, sym) in best.items()}


def _current_month_futures() -> dict[str, str]:
    """Cached per day. The master is refreshed daily, so is this."""
    global _futures, _futures_day
    today_str = date.today().strftime("%Y-%m-%d")
    with _futures_lock:
        if _futures_day == today_str and _futures:
            return _futures
    try:
        loaded = _load_current_month_futures()
    except Exception as e:
        logger.warning(f"tf_future_liquidity_service: future master load failed: {e}")
        return {}
    with _futures_lock:
        _futures, _futures_day = loaded, today_str
        logger.info(f"tf_future_liquidity_service: resolved {len(loaded)} current-month futures")
        return _futures


def future_for(symbol: str) -> str | None:
    """The current-month future for a ranked-list symbol, or None if the stock
    has no futures at all. Applies the corporate-action alias first --
    TATAMOTORS resolves to nothing; the master lists it as TMPV."""
    return _current_month_futures().get(tradable_symbol(symbol))


def compute_liquidity(
    bid: float | None, ask: float | None, ltp: float | None, volume: float | None
) -> tuple[float | None, float | None, float | None]:
    """(spread_pct, spread_rs, turnover_cr) from one quote. Pure, no I/O.

    turnover is volume x price rather than raw volume, because raw volume is not
    comparable across contracts with different lot sizes and prices.

    Returns None rather than 0.0 for anything unquoted -- outside market hours
    bid/ask come back as 0, and a 0.000% spread would read as a perfect book.
    """
    if not bid or not ask or not ltp or ask <= 0 or bid <= 0 or ltp <= 0:
        spread_pct = spread_rs = None
    elif ask < bid:  # crossed book: a stale or malformed quote, not a free lunch
        spread_pct = spread_rs = None
    else:
        spread_rs = round(ask - bid, 4)
        spread_pct = round(spread_rs / ltp * 100, 4)
    turnover_cr = round(volume * ltp / 1e7, 2) if volume and ltp else None
    return spread_pct, spread_rs, turnover_cr


def spread_tier(spread_pct: float | None) -> str | None:
    """'tight' / 'ok' / 'wide', or None when there is nothing to judge."""
    if spread_pct is None:
        return None
    if spread_pct < SPREAD_TIGHT_PCT:
        return "tight"
    return "ok" if spread_pct <= SPREAD_WIDE_PCT else "wide"


def _background_fill(symbols: list[str], auth_token: str, broker: str) -> None:
    today_str = date.today().strftime("%Y-%m-%d")
    try:
        wanted = {s: future_for(s) for s in symbols}
        request = [{"symbol": fut, "exchange": "NFO"} for fut in wanted.values() if fut]
        quotes: dict[str, dict] = {}
        if request:
            # (auth_token, feed_token, broker, symbols) -- feed_token is unused
            # by the multiquote path. The reply is keyed 'results' and is a
            # LIST of {symbol, exchange, data}, not a symbol-keyed dict.
            ok, response, _ = get_multiquotes_with_auth(auth_token, None, broker, request)
            if ok:
                for row in response.get("results") or []:
                    quotes[row.get("symbol", "")] = row.get("data") or {}
            else:
                logger.debug(
                    f"tf_future_liquidity_service: multiquotes said {response.get('message')}"
                )

        now = time.monotonic()
        with _lock:
            for symbol in symbols:
                fut = wanted.get(symbol)
                if fut:
                    q = quotes.get(fut) or {}
                    spread_pct, spread_rs, turnover_cr = compute_liquidity(
                        q.get("bid"), q.get("ask"), q.get("ltp"), q.get("volume")
                    )
                    # Report the median of recent readings rather than this one:
                    # a single reading crosses a tier boundary roughly four
                    # times as often (see SAMPLE_WINDOW).
                    #
                    # Yesterday's readings are dropped rather than aged out, or
                    # the first two minutes of a session would be judged on a
                    # book that closed the evening before. An unquoted tick adds
                    # no sample and reports None -- "not quoting" is the honest
                    # answer, and a stale median dressed as live is not.
                    prior = _cache.get(symbol)
                    if prior and prior[0] != today_str:
                        _samples.pop(symbol, None)
                    if spread_pct is not None:
                        window = _samples.setdefault(symbol, deque(maxlen=SAMPLE_WINDOW))
                        window.append(spread_pct)
                        spread_pct = round(statistics.median(window), 4)
                    _cache[symbol] = (today_str, now, fut, spread_pct, spread_rs, turnover_cr)
                else:
                    # Genuinely has no future. Cached so it is not retried every
                    # refresh, and distinguishable downstream from "not fetched".
                    _cache[symbol] = (today_str, now, "", None, None, None)
                _pending.discard(symbol)
    except Exception as e:
        logger.warning(f"tf_future_liquidity_service: background fill error: {e}")
        with _lock:
            for symbol in symbols:
                _pending.discard(symbol)


def ensure_future_liquidity_cache(
    symbols: list[str], auth_token: str, broker: str, exchange: str = "NSE"
) -> None:
    """Non-blocking. Kicks a background thread for symbols with no entry for
    today or an entry older than REFRESH_INTERVAL_SEC. Safe on every poll --
    fresh or in-flight symbols are skipped, and the whole batch is one broker
    call. `exchange` is accepted for signature parity with the sibling
    enrichers; futures are always looked up on NFO."""
    today_str = date.today().strftime("%Y-%m-%d")
    now = time.monotonic()
    with _lock:
        todo = []
        for s in symbols:
            if s in _pending:
                continue
            cached = _cache.get(s)
            if cached is None or cached[0] != today_str or now - cached[1] > REFRESH_INTERVAL_SEC:
                todo.append(s)
        _pending.update(todo)
    if todo:
        threading.Thread(
            target=_background_fill,
            args=(todo, auth_token, broker),
            daemon=True,
            name="tf-future-liquidity-fill",
        ).start()


def attach_future_liquidity(items: list[dict]) -> list[dict]:
    """Adds 'fut_symbol', 'fut_spread_pct', 'fut_spread_rs', 'fut_turnover_cr'
    and 'fut_tier' to each item in place, from cache. Anything not cached gets
    None across the board rather than a zero."""
    today_str = date.today().strftime("%Y-%m-%d")
    with _lock:
        for item in items:
            cached = _cache.get(item.get("symbol", ""))
            if not cached or cached[0] != today_str:
                item["fut_symbol"] = None
                item["fut_spread_pct"] = None
                item["fut_spread_rs"] = None
                item["fut_turnover_cr"] = None
                item["fut_tier"] = None
                continue
            _, _, fut, spread_pct, spread_rs, turnover_cr = cached
            item["fut_symbol"] = fut or None
            item["fut_spread_pct"] = spread_pct
            item["fut_spread_rs"] = spread_rs
            item["fut_turnover_cr"] = turnover_cr
            item["fut_tier"] = spread_tier(spread_pct)
    return items


def _demo() -> None:
    """Self-check for the pure parts -- no broker, no database."""
    # A real quote: RELIANCE29SEP26FUT as measured 22-Sep-2026.
    spread_pct, spread_rs, turnover_cr = compute_liquidity(1248.6, 1248.8, 1248.7, 12_157_500)
    assert spread_rs == 0.2, spread_rs
    assert abs(spread_pct - 0.016) < 0.001, spread_pct
    assert turnover_cr and turnover_cr > 0

    # Outside market hours everything comes back zero. That must NOT read as a
    # perfect book, which is what a naive (ask-bid)/ltp would report.
    assert compute_liquidity(0, 0, 0, 0) == (None, None, None)
    assert compute_liquidity(None, None, 100.0, 5) == (None, None, 0.0)

    # A crossed book is a stale quote, not a zero spread.
    assert compute_liquidity(101.0, 100.0, 100.0, 5)[0] is None

    assert spread_tier(0.010) == "tight"
    assert spread_tier(0.040) == "ok"
    assert spread_tier(0.099) == "wide"  # SOLARINDS, the case that motivated this
    assert spread_tier(None) is None
    # Boundaries are closed on the 'ok' side, so neither edge falls through.
    assert spread_tier(SPREAD_TIGHT_PCT) == "ok"
    assert spread_tier(SPREAD_WIDE_PCT) == "ok"

    logger.info("tf_future_liquidity_service self-check passed")


if __name__ == "__main__":
    _demo()
