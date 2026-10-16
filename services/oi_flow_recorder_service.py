"""
OI Flow recorder: the per-minute record Writer Flow reads.

Every minute its options market is open (from the market calendar), for each configured
underlying, one option-chain call (the same one /api/v1/optionchain serves)
records the open interest, premium and volume of every contract within
OI_FLOW_STRIKES_AROUND_ATM strikes of ATM into db/oi_flow.duckdb. That is one
broker call per underlying per minute: about 4% of Upstox's half-hour
allowance for three underlyings.

Opt-in (OI_FLOW_RECORDER_ENABLED=true, wired in app.py) and fully separate
from the OI Profile: its own scheduler, its own database, no shared state.

Minutes it did not catch are filled from one-minute history, which carries OI
per candle on NFO, BFO and MCX (checked 16-Oct-2026): the morning, once, when
it is started mid-day, and any minute it missed after that, every five
minutes. A minute is missed when the process is starved: on 16-Oct-2026 a
machine at a load average of 103 held the live beat back for six minutes, and
nothing ever went back for them. The history service paces those calls behind
its shared gate, and rows the live recorder already holds are left alone.

A strike that drifts out of the window as ATM moves keeps what was recorded
for it; it is simply not sampled again until it is back inside.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from database.oi_flow_db import (
    claim_writer,
    contracts_between,
    init_oi_flow_database,
    insert_samples,
    recorded_minutes,
    release_writer,
)
from utils.logging import get_logger

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
TICK_JOB_ID = "oi_flow_tick"
BACKFILL_JOB_ID = "oi_flow_backfill"
GAP_JOB_ID = "oi_flow_gaps"
GAP_CLOSE_JOB_ID = "oi_flow_gaps_close"
# The newest minutes still belong to the live beat (it samples at second 5 and
# can run late), so a gap is only a gap once it is this old.
GAP_SETTLE_MINUTES = 2
# History either has a minute or it does not; asking a third time costs a call
# per contract for nothing. A holiday or a broker outage stops after two.
GAP_MAX_TRIES = 2
# Contracts recorded this close to a gap are the window it is filled for.
GAP_MARGIN_MINUTES = 5
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()
# (underlying, trade_date) -> expiry, so the expiry list is asked once a day.
_expiry_cache: dict[tuple[str, date], str] = {}
# (underlying, minute) -> times a fill has asked history for it. Today only:
# cleared when the day changes, so it holds at most a session of minutes.
_gap_tries: dict[tuple[str, datetime], int] = {}
_gap_day: date | None = None
# The startup fill and the five-minute fill are separate jobs; one at a time,
# or both would ask history for the same contracts.
_fill_lock = threading.Lock()
# (options exchange, day) -> (open, close) or None when shut. Today's only:
# cleared when the day changes, so it holds one entry per exchange.
_sessions: dict[tuple[str, date], tuple[dtime, dtime] | None] = {}
_sessions_lock = threading.Lock()


def configured_underlyings() -> list[tuple[str, str]]:
    """OI_FLOW_UNDERLYINGS as (underlying, its quote exchange) pairs."""
    raw = os.getenv("OI_FLOW_UNDERLYINGS", "NIFTY:NSE_INDEX,BANKNIFTY:NSE_INDEX,SENSEX:BSE_INDEX")
    pairs = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, exchange = item.partition(":")
        pairs.append((name.strip().upper(), (exchange or "NSE_INDEX").strip().upper()))
    return pairs


def strikes_around_atm() -> int:
    try:
        return max(1, min(50, int(os.getenv("OI_FLOW_STRIKES_AROUND_ATM", "15"))))
    except ValueError:
        return 15


def options_exchange(quote_exchange: str) -> str:
    """Where the underlying's options trade (NSE_INDEX -> NFO, MCX -> MCX), as OpenAlgo maps it."""
    from services.option_symbol_service import get_option_exchange

    return get_option_exchange(quote_exchange)


def _default_window(day: date, exchange: str) -> tuple[dtime, dtime] | None:
    """The calendar module's own default hours, for when its database cannot be read."""
    from database.market_calendar_db import DEFAULT_MARKET_TIMINGS

    timings = DEFAULT_MARKET_TIMINGS.get(exchange)
    if day.weekday() >= 5 or not timings:
        return None

    def at(ms: int) -> dtime:
        return dtime(ms // 3_600_000, ms // 60_000 % 60)

    return at(timings["start_offset"]), at(timings["end_offset"])


def _calendar_window(day: date, exchange: str) -> tuple[dtime, dtime] | None:
    """The calendar's session for an exchange on a day, or None when it is shut."""
    from database.market_calendar_db import db_session, get_effective_session_window

    try:
        window = get_effective_session_window(day, exchange)
    finally:
        # A scheduler thread outlives any request, so nothing else removes it.
        db_session.remove()
    if window is None:
        return None
    start = datetime.fromtimestamp(window["start_ms"] / 1000, IST)
    end = datetime.fromtimestamp(window["end_ms"] / 1000, IST)
    return start.time().replace(second=0, microsecond=0), end.time().replace(
        second=0, microsecond=0
    )


def session_for(quote_exchange: str, day: date) -> tuple[dtime, dtime] | None:
    """Where the underlying's options trade, when that market is open on `day`; None when shut."""
    exchange = options_exchange(quote_exchange)
    key = (exchange, day)
    # The live beat and the gap filler run on separate scheduler threads, real
    # ones under gthread: check, clear and fill under one lock, or one thread
    # clearing for a new day drops what the other just stored. Held through
    # the calendar read, which happens once per exchange per day.
    with _sessions_lock:
        if any(d != day for _, d in _sessions):
            _sessions.clear()
        if key not in _sessions:
            try:
                _sessions[key] = _calendar_window(day, exchange)
            except Exception:
                logger.exception(f"oi_flow: reading the {exchange} trading hours failed")
                return _default_window(day, exchange)
            if _sessions[key] is None:
                logger.info(f"oi_flow: {exchange} is not trading on {day}, nothing to record there")
        return _sessions[key]


def within_session(now_ist: datetime, quote_exchange: str = "NSE_INDEX") -> bool:
    session = session_for(quote_exchange, now_ist.date())
    return session is not None and session[0] <= now_ist.time() <= session[1]


def _api_key() -> str | None:
    from database.auth_db import get_first_available_api_key

    return get_first_available_api_key()


def nearest_expiry(underlying: str, quote_exchange: str, api_key: str, today: date) -> str | None:
    """Nearest option expiry as DDMMMYY, asked once per underlying per day."""
    key = (underlying, today)
    if key in _expiry_cache:
        return _expiry_cache[key]
    from services.expiry_service import get_expiry_dates

    ok, body, _ = get_expiry_dates(underlying, options_exchange(quote_exchange), "options", api_key)
    expiries = body.get("data") if ok else None
    if not expiries:
        return None
    expiry = str(expiries[0]).replace("-", "").upper()
    _expiry_cache[key] = expiry
    return expiry


def _finite(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def chain_samples(
    chain: dict,
    underlying: str,
    quote_exchange: str,
    expiry: str,
    sample_time: datetime,
    source: str,
) -> list[dict]:
    """Rows for insert_samples from one option-chain answer."""
    spot = _finite(chain.get("underlying_ltp"))
    rows = []
    for row in chain.get("chain") or []:
        strike = _finite(row.get("strike"))
        if strike is None:
            continue
        for side in ("ce", "pe"):
            leg = row.get(side) or {}
            if not leg.get("symbol"):
                continue
            oi = _finite(leg.get("oi"))
            ltp = _finite(leg.get("ltp"))
            # The option chain answers 0 for a contract whose quote did not come
            # back. With neither open interest nor a price there is no reading,
            # so nothing is stored and the minute stays a gap for that contract,
            # which the analysis bridges, instead of a 0 it would read as every
            # writer leaving.
            if oi is None or ltp is None or (oi <= 0 and ltp <= 0):
                continue
            rows.append(
                {
                    "trade_date": sample_time.date(),
                    "sample_time": sample_time,
                    "underlying": underlying,
                    "exchange": options_exchange(quote_exchange),
                    "expiry": expiry,
                    "strike": strike,
                    "side": side.upper(),
                    "symbol": leg["symbol"],
                    "oi": int(oi),
                    "ltp": ltp,
                    "volume": int(_finite(leg.get("volume")) or 0),
                    "spot": spot if spot and spot > 0 else None,
                    "lot_size": int(leg.get("lotsize") or 0) or None,
                    "source": source,
                }
            )
    return rows


def record_once(now_ist: datetime | None = None) -> int:
    """One sample of every configured underlying. Never raises: a scheduler job
    that raised would stop the minute beat for the rest of the day."""
    now_ist = now_ist or datetime.now(IST)
    # Each underlying records while its own market is open: NIFTY options to
    # 15:40, CRUDEOIL options to 23:55.
    active = [(u, q) for u, q in configured_underlyings() if within_session(now_ist, q)]
    if not active:
        return 0
    api_key = _api_key()
    if not api_key:
        logger.debug("oi_flow: no API key yet, skipping this minute")
        return 0
    from services.option_chain_service import get_option_chain

    sample_time = now_ist.replace(second=0, microsecond=0, tzinfo=None)
    written = 0
    stored: set[str] = set()
    for underlying, quote_exchange in active:
        try:
            expiry = nearest_expiry(underlying, quote_exchange, api_key, sample_time.date())
            if not expiry:
                logger.warning(
                    f"oi_flow: no option expiry found for {underlying}, skipping it this minute"
                )
                continue
            ok, chain, _ = get_option_chain(
                underlying, quote_exchange, expiry, strikes_around_atm(), api_key
            )
            if not ok:
                logger.warning(
                    f"oi_flow: option chain for {underlying} was not available this minute"
                )
                continue
            written += insert_samples(
                chain_samples(chain, underlying, quote_exchange, expiry, sample_time, "live")
            )
            stored.add(underlying)
        except Exception:
            logger.exception(f"oi_flow: recording {underlying} failed this minute")
    _rebuild_watched(stored)
    return written


def _rebuild_watched(underlyings: set[str]) -> None:
    """Rebuild the Writer Flow views someone is watching, so their next refresh is served from memory."""
    if not underlyings:
        return
    from services.writer_flow_service import rebuild_watched

    rebuild_watched(underlyings)


def history_samples(
    records: list[dict],
    leg: dict,
    spot_by_minute: dict[datetime, float],
    wanted: set[datetime] | None = None,
    session: tuple[dtime, dtime] | None = None,
) -> list[dict]:
    """Backfill rows from one contract's one-minute history, for `wanted` minutes only if given."""
    rows = []
    for r in records:
        ts = r.get("timestamp")
        if ts is None:
            continue
        # A candle holds the state at the END of its minute, which is what
        # the live recorder reads a minute later: measured on 16-Oct-2026,
        # NIFTY 23100 CE, live OI at t equalled the candle labelled t-1 in 25
        # of 35 minutes and the candle labelled t in 7. So a candle is stored
        # at its minute plus one, or every filled minute sat a minute early.
        t = datetime.fromtimestamp(int(ts), IST).replace(
            tzinfo=None, second=0, microsecond=0
        ) + timedelta(minutes=1)
        if session is not None and not (session[0] <= t.time() <= session[1]):
            continue
        if wanted is not None and t not in wanted:
            continue
        # A candle with no OI reading is not a reading of zero: stored as 0 it
        # read as every writer leaving the strike in one minute.
        oi = _finite(r.get("oi"))
        close = _finite(r.get("close"))
        if oi is None or close is None:
            continue
        rows.append(
            {
                **leg,
                "trade_date": t.date(),
                "sample_time": t,
                "oi": int(oi),
                "ltp": close,
                "volume": int(_finite(r.get("volume")) or 0),
                # The underlying's own candle for the same minute, shifted alike.
                "spot": spot_by_minute.get(t),
                "source": "backfill",
            }
        )
    return rows


def missing_minutes(
    held: set[datetime],
    today: date,
    now: datetime,
    session: tuple[dtime, dtime],
) -> list[datetime]:
    """Session minutes from the open up to GAP_SETTLE_MINUTES ago that hold no sample."""
    last = min(
        now.replace(second=0, microsecond=0) - timedelta(minutes=GAP_SETTLE_MINUTES),
        datetime.combine(today, session[1]),
    )
    out = []
    t = datetime.combine(today, session[0])
    while t <= last:
        if t not in held:
            out.append(t)
        t += timedelta(minutes=1)
    return out


def fill_gaps(now_ist: datetime | None = None) -> int:
    """Fill today's missing minutes from one-minute history. Never raises.

    The startup backfill is this with the whole morning missing. Returns the
    number of rows offered for storage.
    """
    global _gap_day
    now_ist = now_ist or datetime.now(IST)
    if not _fill_lock.acquire(blocking=False):
        return 0
    try:
        today = now_ist.date()
        if _gap_day != today:
            _gap_tries.clear()
            _gap_day = today
        api_key = _api_key()
        if not api_key:
            return 0
        written = 0
        stored: set[str] = set()
        for underlying, quote_exchange in configured_underlyings():
            try:
                filled = _fill_underlying(underlying, quote_exchange, api_key, now_ist)
                written += filled
                if filled:
                    stored.add(underlying)
            except Exception:
                logger.exception(f"oi_flow: filling missed minutes of {underlying} failed")
        _rebuild_watched(stored)
        return written
    finally:
        _fill_lock.release()


def _fill_underlying(underlying: str, quote_exchange: str, api_key: str, now_ist: datetime) -> int:
    from services.history_service import get_history
    from services.option_chain_service import get_option_chain

    today = now_ist.date()
    now = now_ist.replace(tzinfo=None)
    session = session_for(quote_exchange, today)
    if session is None or now.time() < session[0]:
        return 0
    expiry = nearest_expiry(underlying, quote_exchange, api_key, today)
    if not expiry:
        return 0
    missing = [
        m
        for m in missing_minutes(recorded_minutes(underlying, expiry, today), today, now, session)
        if _gap_tries.get((underlying, m), 0) < GAP_MAX_TRIES
    ]
    if not missing:
        return 0
    for m in missing:
        _gap_tries[(underlying, m)] = _gap_tries.get((underlying, m), 0) + 1

    margin = timedelta(minutes=GAP_MARGIN_MINUTES)
    legs = contracts_between(underlying, expiry, today, missing[0] - margin, missing[-1] + margin)
    if not legs:
        # Nothing recorded near the gap (a cold start): the window as it is now.
        ok, chain, _ = get_option_chain(
            underlying, quote_exchange, expiry, strikes_around_atm(), api_key, with_quotes=False
        )
        if not ok:
            return 0
        legs = chain_samples(chain, underlying, quote_exchange, expiry, now, "backfill")

    day = today.strftime("%Y-%m-%d")
    spot_symbol, spot_exchange = underlying, quote_exchange
    if quote_exchange == "MCX":
        # MCX lists no spot: its options are priced against the near-month
        # future, which is what the live chain reports as the underlying too.
        from services.option_symbol_service import resolve_underlying_quote

        resolved = resolve_underlying_quote(underlying, "MCX")
        if resolved:
            spot_symbol, spot_exchange = resolved
    ok, spot_body, _ = get_history(
        spot_symbol, spot_exchange, "1m", day, day, api_key=api_key, background=True
    )
    spot_by_minute = {}
    for r in (spot_body.get("data") or []) if ok else []:
        close = _finite(r.get("close"))
        if close is None or close <= 0:
            continue
        t = datetime.fromtimestamp(int(r["timestamp"]), IST).replace(
            tzinfo=None, second=0, microsecond=0
        ) + timedelta(minutes=1)
        spot_by_minute[t] = close

    wanted = set(missing)
    written = 0
    for leg in legs:
        base = {
            "underlying": underlying,
            "exchange": leg["exchange"],
            "expiry": expiry,
            "strike": leg["strike"],
            "side": leg["side"],
            "symbol": leg["symbol"],
            "lot_size": leg["lot_size"],
        }
        ok, body, _ = get_history(
            leg["symbol"], leg["exchange"], "1m", day, day, api_key=api_key, background=True
        )
        if ok:
            written += insert_samples(
                history_samples(body.get("data") or [], base, spot_by_minute, wanted, session)
            )
    filled = len(wanted & recorded_minutes(underlying, expiry, today))
    logger.info(
        f"oi_flow: {underlying} {expiry} had {len(missing)} missed minutes, "
        f"filled {filled} from history"
    )
    return written


def backfill_today(now_ist: datetime | None = None) -> int:
    """The startup fill: every minute since the open that is not held. Never raises."""
    return fill_gaps(now_ist)


def init_oi_flow_recorder() -> None:
    """Idempotent start: database, minute beat, and one backfill shortly after."""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        if not claim_writer():
            logger.warning(
                "Writer Flow recorder not started: another OpenAlgo process is already "
                "recording into the same Writer Flow database. Stop the other copy, "
                "or give each its own OI_FLOW_DATABASE_PATH, and restart."
            )
            return
        init_oi_flow_database()
        _scheduler = BackgroundScheduler(timezone=IST)
        _scheduler.add_job(
            record_once,
            # Every minute from 09:00 to 23:59, any day: the market calendar
            # decides which underlyings are open (weekends, holidays, the MCX
            # evening session and special sessions included).
            trigger=CronTrigger(hour="9-23", minute="*", second=5, timezone=IST),
            id=TICK_JOB_ID,
            # The same reasoning as the TradeFinder snapshot: with one slot, a
            # minute that overran silently took the next minute with it.
            max_instances=2,
            coalesce=True,
            misfire_grace_time=50,
        )
        _scheduler.add_job(
            backfill_today,
            trigger=DateTrigger(run_date=datetime.now(IST) + timedelta(seconds=20)),
            id=BACKFILL_JOB_ID,
        )
        # Minutes the beat missed, every five minutes through every session
        # (the run after F&O's 15:40 close checks its last minutes), and once
        # at 23:58 for MCX's 23:55 close, which the 23:55 run cannot reach
        # past its settle time. No gaps, no broker calls.
        for job_id, minute in ((GAP_JOB_ID, "*/5"), (GAP_CLOSE_JOB_ID, "58")):
            _scheduler.add_job(
                fill_gaps,
                trigger=CronTrigger(
                    hour="23" if job_id == GAP_CLOSE_JOB_ID else "9-23",
                    minute=minute,
                    second=35,
                    timezone=IST,
                ),
                id=job_id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=240,
            )
        _scheduler.start()
        logger.info(
            f"OI Flow recorder started: {', '.join(u for u, _ in configured_underlyings())}, "
            f"ATM +-{strikes_around_atm()} strikes, every minute each one's market is open"
        )


def shutdown_oi_flow_recorder() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
        release_writer()
