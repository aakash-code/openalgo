"""The market read, kept up to date every minute the recorder stores.

The reading itself is ``services/market_read/core.py``. This module feeds it
and delivers what it finds:

* After the OI Flow recorder stores a minute of NIFTY, BANKNIFTY or SENSEX it
  calls :func:`on_minute`, which only hands the work to this module's own
  thread and returns. The recorder's beat never waits on a read.
* Once a minute, one batch quote asks for the current- and next-month NIFTY
  futures, India VIX and the index (for its previous close): about 375 broker
  calls a day. The first read of a day, and every FILL_EVERY minutes after it,
  fills any minute the quotes missed from one-minute history, paced as
  background work, so a restart mid-session still reads the whole day.
* Each read is recomputed from the whole day, so it never depends on what this
  process saw. Events are stored with stable ids; only ones not already stored
  are announced, over Socket.IO as ``market_read_update`` and, for the major
  ones, on Telegram.

Three switches (read, Telegram, sound) live in the record's own settings table.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from utils import real_threading, stream_registry
from utils.logging import get_logger

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

UPDATE_EVENT = "market_read_update"
VIX = ("INDIAVIX", "NSE_INDEX")
OTHERS = ("BANKNIFTY", "SENSEX")
FILL_EVERY = timedelta(minutes=15)
TELEGRAM_EVERY = timedelta(minutes=10)
# An event first seen this long after it happened is history, not news: a
# restart that reads the morning again stores those events and sends nothing.
TELEGRAM_FRESH = timedelta(minutes=5)

SETTINGS = {"enabled": "true", "telegram": "true", "sound": "true"}


def main_underlying() -> str:
    return os.getenv("MARKET_READ_UNDERLYING", "NIFTY").strip().upper() or "NIFTY"


def _watched() -> set[str]:
    return {main_underlying(), *OTHERS}


# ------------------------------------------------------------------ settings

_settings_lock = real_threading.Lock()
_settings: dict[str, bool] | None = None


def settings() -> dict[str, bool]:
    """The three switches, read once and then from memory."""
    global _settings
    with _settings_lock:
        if _settings is not None:
            return dict(_settings)
    from database.oi_flow_db import read_setting

    values = {}
    for name, default in SETTINGS.items():
        try:
            stored = read_setting(f"market_read_{name}")
        except Exception:
            logger.exception("Market read: could not read its switches, using the defaults")
            stored = None
        values[name] = (stored if stored is not None else default) == "true"
    with _settings_lock:
        if _settings is None:
            _settings = values
        return dict(_settings)


def update_settings(changes: dict[str, bool]) -> dict[str, bool]:
    """Store and apply switches. Unknown names are refused with ValueError."""
    global _settings
    from database.oi_flow_db import write_setting

    unknown = set(changes) - set(SETTINGS)
    if unknown:
        raise ValueError(f"Unknown market read switch: {', '.join(sorted(unknown))}")
    current = settings()
    for name, on in changes.items():
        write_setting(f"market_read_{name}", "true" if on else "false")
        current[name] = bool(on)
    with _settings_lock:
        _settings = current
    return dict(current)


# -------------------------------------------------------------------- worker

_jobs = real_threading.Queue()
_worker = None
_worker_lock = real_threading.Lock()
_queued = False  # one read waiting is enough: it reads everything stored by then
_STOP = object()

# The latest read of today, served to the panel without recomputing it.
_latest_lock = real_threading.Lock()
_latest: dict | None = None


def on_minute(underlying: str) -> None:
    """Called by the recorder after it stores a minute. Never raises, never waits."""
    global _queued
    try:
        if underlying not in _watched() or not settings()["enabled"]:
            return
        with _worker_lock:
            if _queued:
                return
            _queued = True
        _ensure_worker()
        _jobs.put(underlying)
    except Exception:
        logger.exception("Market read: could not queue this minute's read")


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = real_threading.Thread(target=_loop, name="market-read", daemon=True)
            _worker.start()


# Longest one read may take on the hub before this thread stops waiting for it.
# A read is seconds; the history fill every 15 minutes is paced background work.
READ_TIMEOUT = 300


def _read_and_release() -> None:
    try:
        update()
    finally:
        # The read looks up the futures and the API key through scoped sessions
        # every minute, on a thread no request teardown reaches.
        from utils.db_sessions import remove_all_scoped_sessions

        remove_all_scoped_sessions()


def _loop() -> None:
    global _queued
    while True:
        job = _jobs.get()
        if job is _STOP:
            return
        with _worker_lock:
            _queued = False
        if stream_registry.should_stop():
            continue
        try:
            # This thread is real, and the read reaches database sessions, the
            # broker client and quotes, all green under eventlet. Called from
            # here it waited on the hub forever (22-Oct, from 10:52) and every
            # later minute found a read still queued. run_on_hub calls it
            # inline on the gthread worker and the dev server.
            real_threading.run_on_hub(_read_and_release, timeout=READ_TIMEOUT)
        except TimeoutError:
            logger.warning(
                f"Market read: a read took over {READ_TIMEOUT}s; the next minute starts a new one"
            )
        except Exception:
            logger.exception("Market read: this minute's read failed")


def stop_worker() -> None:
    """Let the worker finish. Called when the recorder shuts down."""
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None and worker.is_alive():
        _jobs.put(_STOP)
        real_threading.join(worker, timeout=5)


# ------------------------------------------------------------------ market data

_FUT_RE = re.compile(r"^(?P<base>[A-Z0-9&-]+?)(?P<expiry>\d{2}[A-Z]{3}\d{2})FUT$")


def _futures_pair(base: str) -> list[dict]:
    """The current and next month futures, nearest first."""
    from services.option_symbol_service import near_month_futures

    return near_month_futures(base, "NFO", count=2)


def _quote_targets(base: str) -> list[tuple[str, str]]:
    return [(f["symbol"], f["exchange"]) for f in _futures_pair(base)] + [VIX, (base, "NSE_INDEX")]


_quoted_minute: datetime | None = None
_filled_at: dict[date, datetime] = {}


def _record_quotes(base: str, minute: datetime, api_key: str) -> None:
    """One batch quote for this minute, once."""
    global _quoted_minute
    if _quoted_minute == minute:
        return
    from database.oi_flow_db import insert_market_quotes
    from services.quotes_service import get_multiquotes

    targets = _quote_targets(base)
    ok, body, _ = get_multiquotes(
        [{"symbol": s, "exchange": e} for s, e in targets], api_key=api_key
    )
    if not ok:
        logger.warning("Market read: the futures and India VIX quotes were not available")
        return
    _quoted_minute = minute
    rows = []
    for item in body.get("results") or []:
        data = item.get("data") or {}
        ltp = data.get("ltp")
        if not ltp:
            continue
        rows.append(
            {
                "trade_date": minute.date(),
                "sample_time": minute,
                "symbol": item.get("symbol"),
                "exchange": item.get("exchange"),
                "ltp": float(ltp),
                "oi": int(data.get("oi") or 0) or None,
                "volume": int(data.get("volume") or 0) or None,
                "prev_close": float(data.get("prev_close") or 0) or None,
                "source": "live",
            }
        )
    insert_market_quotes(rows)


def _fill_from_history(base: str, now: datetime, api_key: str) -> None:
    """Minutes the live quotes missed today, from one-minute history."""
    day = now.date()
    last = _filled_at.get(day)
    if last is not None and now - last < FILL_EVERY:
        return
    _filled_at.clear()
    _filled_at[day] = now
    from database.oi_flow_db import insert_market_quotes
    from services.history_service import get_history

    text = day.strftime("%Y-%m-%d")
    for symbol, exchange in _quote_targets(base)[:-1]:  # the index's own close comes live
        if stream_registry.should_stop():
            return
        ok, body, _ = get_history(
            symbol, exchange, "1m", text, text, api_key=api_key, background=True
        )
        if not ok:
            continue
        rows = []
        for r in body.get("data") or []:
            close = r.get("close")
            if not close:
                continue
            # A candle's close is the state at its end: stamped one minute on,
            # as the recorder stamps a backfilled option minute.
            t = datetime.fromtimestamp(int(r["timestamp"]), IST).replace(
                tzinfo=None, second=0, microsecond=0
            ) + timedelta(minutes=1)
            if t > now:
                continue
            rows.append(
                {
                    "trade_date": day,
                    "sample_time": t,
                    "symbol": symbol,
                    "exchange": exchange,
                    "ltp": float(close),
                    "oi": int(r.get("oi") or 0) or None,
                    "volume": int(r.get("volume") or 0) or None,
                    "prev_close": None,
                    "source": "backfill",
                }
            )
        insert_market_quotes(rows)


def market_minutes(base: str, quotes: list[dict]):
    """Stored quotes as the core's MarketMinute list, plus the index's previous close."""
    from services.market_read.core import MarketMinute

    futures = sorted(
        {q["symbol"] for q in quotes if _FUT_RE.match(q["symbol"] or "")},
        key=lambda s: datetime.strptime(_FUT_RE.match(s)["expiry"], "%d%b%y"),
    )
    current = futures[0] if futures else None
    by_minute: dict[datetime, dict[str, dict]] = {}
    prev_close = None
    for q in quotes:
        by_minute.setdefault(q["sample_time"], {})[q["symbol"]] = q
        if q["symbol"] == base and q.get("prev_close"):
            prev_close = q["prev_close"]
    out, last_oi = [], {}
    for t in sorted(by_minute):
        row = by_minute[t]
        for s in futures:
            if row.get(s, {}).get("oi"):
                last_oi[s] = row[s]["oi"]
        fut = row.get(current, {}).get("ltp") if current else None
        # Both months together, so a rollover does not read as positions closing.
        oi = sum(last_oi.values()) if len(last_oi) == len(futures) and futures else None
        vix = row.get(VIX[0], {}).get("ltp")
        out.append(MarketMinute(time=t, fut=fut, fut_oi=oi, vix=vix))
    return out, prev_close


# ---------------------------------------------------------------------- read


def _expiry_at(expiry: str) -> datetime:
    return datetime.strptime(expiry, "%d%b%y").replace(hour=15, minute=30)


def compute(day: date) -> dict | None:
    """The read for a day from what is stored. Makes no broker call."""
    from database.oi_flow_db import expiry_for_day, load_day, load_market_quotes
    from services.market_read.core import ReadInputs, build_read
    from services.option_greeks_service import calculate_chain_greeks

    base = main_underlying()
    expiry = expiry_for_day(base, day)
    if not expiry:
        return None
    others = {}
    for name in OTHERS:
        other_expiry = expiry_for_day(name, day)
        if other_expiry:
            others[name] = load_day(name, other_expiry, day)
    market, prev_close = market_minutes(base, load_market_quotes(day))
    read = build_read(
        ReadInputs(
            underlying=base,
            samples=load_day(base, expiry, day),
            expiry_at=_expiry_at(expiry),
            market=market,
            prev_close=prev_close,
            others=others,
            greeks=partial(calculate_chain_greeks),
        )
    )
    if read is not None:
        read["date"] = day
        read["expiry"] = expiry
    return read


def update(now: datetime | None = None) -> dict | None:
    """Read today, store and announce what is new. Runs on the worker thread."""
    global _latest
    from database.auth_db import get_first_available_api_key
    from database.oi_flow_db import store_read_events, store_read_minute

    now = (now or datetime.now(IST)).replace(tzinfo=None)
    minute = now.replace(second=0, microsecond=0)
    base = main_underlying()
    api_key = get_first_available_api_key()
    if api_key:
        try:
            _record_quotes(base, minute, api_key)
            _fill_from_history(base, now, api_key)
        except Exception:
            logger.exception("Market read: fetching the futures and India VIX failed")
    read = compute(minute.date())
    if read is None:
        return None
    new = store_read_events(base, minute.date(), read["events"])
    store_read_minute(base, minute.date(), read["minute"], json.dumps(read["state"], default=str))
    with _latest_lock:
        _latest = read
    _announce(read, new)
    if settings()["telegram"]:
        # Called every minute, new events or not: an event held back by the
        # 10-minute spacing goes out at the first read after the spacing ends,
        # not only when another event happens to follow it.
        _queue_telegram(
            [e for e in new if e["level"] == "major" and now - e["time"] <= TELEGRAM_FRESH],
            now=now,
        )
    else:
        with _telegram_lock:
            _telegram_pending.clear()  # switched off: nothing held back goes out later
    return read


def latest(day: date) -> dict | None:
    with _latest_lock:
        if _latest is not None and _latest.get("date") == day:
            return _latest
    return None


# ------------------------------------------------------------------- delivery


def _epoch(t: datetime) -> int:
    return int(t.replace(tzinfo=IST).timestamp())


def event_json(e: dict) -> dict:
    return {**e, "time": e["time"].strftime("%H:%M"), "epoch": _epoch(e["time"])}


def read_json(read: dict) -> dict:
    """The read as the panel receives it: times as HH:MM, with epochs for the chart."""
    return {
        "underlying": read["underlying"],
        "expiry": read.get("expiry"),
        "date": read["date"].isoformat(),
        "minute": read["minute"].strftime("%H:%M"),
        "headline": read["headline"],
        "state": read["state"],
        "events": [event_json(e) for e in read["events"]],
        "series": [
            {**s, "time": s["time"].strftime("%H:%M"), "epoch": _epoch(s["time"])}
            for s in read["series"]
        ],
    }


def _announce(read: dict, new: list[dict]) -> None:
    try:
        from extensions import emit_from_any_thread

        emit_from_any_thread(
            UPDATE_EVENT,
            {
                "underlying": read["underlying"],
                "minute": read["minute"].strftime("%H:%M"),
                "headline": read["headline"],
                "events": [event_json(e) for e in new],
                # Whether the page should sound and pop up on a major event.
                "sound": settings()["sound"],
            },
        )
    except Exception:
        logger.exception("Market read: could not announce the new read")


_telegram_lock = real_threading.Lock()
_telegram_pending: list[dict] = []
_telegram_last: datetime | None = None


def _queue_telegram(events: list[dict], now: datetime | None = None) -> None:
    """Major events to Telegram, at most one message every TELEGRAM_EVERY.

    Events raised in between wait and go together in the next message, so a
    busy stretch sends one message rather than one per event.
    """
    global _telegram_last
    now = now or datetime.now(IST).replace(tzinfo=None)
    with _telegram_lock:
        _telegram_pending.extend(events)
        if not _telegram_pending:
            return
        if _telegram_last is not None and now - _telegram_last < TELEGRAM_EVERY:
            return
        batch = list(_telegram_pending)
        _telegram_pending.clear()
        _telegram_last = now
    _send_telegram(telegram_text(batch))


_MARKDOWN = re.compile(r"([_*`\[])")


def telegram_text(events: list[dict]) -> str:
    """The message, escaped for the bot's Markdown: a stray _ or * fails the send."""
    lines = [f"{main_underlying()} market read"]
    for e in events:
        lines.append(f"{e['time']:%H:%M}  {e['title']}\n{e['reason']}")
    return _MARKDOWN.sub(r"\\\1", "\n\n".join(lines))


def _send_telegram(text: str) -> None:
    try:
        from database.auth_db import get_first_available_api_key, verify_api_key
        from database.telegram_db import get_telegram_user_by_username
        from services.telegram_alert_service import alert_executor, telegram_alert_service

        if not telegram_alert_service.is_bot_active():
            return
        api_key = get_first_available_api_key()
        username = verify_api_key(api_key) if api_key else None
        user = get_telegram_user_by_username(username) if username else None
        if not user or not user.get("notifications_enabled"):
            return
        alert_executor.submit(telegram_alert_service.send_alert_sync, user["telegram_id"], text)
    except Exception:
        logger.exception("Market read: could not send the Telegram message")
