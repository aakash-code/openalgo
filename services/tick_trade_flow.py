"""
Who traded each option, minute by minute, from the tick store.

Writer Flow reads an OI step as writing, covering, buying or unwinding. With
only the premium to go on it guesses the side: OI up while the premium falls
reads as writing. The tick store (services/tick_capture_service) keeps every
trade with the bid and offer standing before it, so the side can be read from
the trades instead: a trade at or above the offer was a buyer in a hurry, one
at or below the bid a seller (the quote rule). Trades inside the spread are
kept apart, as unplaced, and never guessed.

The result is each contract's (bought, sold, unplaced) volume per minute,
keyed by symbol and the minute's start in IST wall clock, the clock the OI
Flow samples use. services.oi_flow_analysis.analyse reads it.

Cost. A whole NIFTY day is about 2.7 million ticks and takes about 4 s of
DuckDB on the server's 2 cores. A finished minute never changes, so each
(day, underlying) is read once and after that only from a little before the
last minute already read; the last two minutes are read again every time,
because the capture writes in batches and a minute may still be filling.
DuckDB runs on one thread so the trading path keeps the other core.

What the ticks hold, measured on 18-20 Nov 2026 (Upstox, NIFTY):
- Half the rows carry no bid or offer (price-only packets), so the quote
  standing before a trade is the last one any row carried.
- Volume is cumulative for the day; a trade is a rise in it.
- ts_recv (arrival) is used, not ts_exch: a quiet strike can carry an
  exchange time from an earlier day, and arrival trails the exchange by a
  median 123 ms, well inside a minute.
"""

from __future__ import annotations

import glob
import os
from datetime import date, datetime

import pytz

from database.ticks_store import _duck, _ql, data_dir
from utils import real_threading
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
MINUTE_MS = 60_000
# Read again each refresh: the capture flushes in batches, so these may still fill.
REREAD_MINUTES = 2
# Ticks before the first minute read, only to know the quote standing then.
SEED_MINUTES = 2
DAYS_KEPT = 8

Minutes = dict[datetime, tuple[int, int, int]]

_days = LockedTTLCache(maxsize=DAYS_KEPT, ttl=24 * 3600)
_lock = real_threading.Lock()


def read_trades_default() -> bool:
    """Whether Writer Flow reads trades unless a request says otherwise."""
    return os.getenv("WRITER_FLOW_READ_TRADES", "false").strip().lower() == "true"


def _partition_glob(day: date, underlying: str) -> str:
    return os.path.join(
        data_dir(), f"date={day.isoformat()}", f"underlying={underlying}", "*.parquet"
    )


def _minute(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, IST).replace(tzinfo=None)


def _sql(path_glob: str, from_ms: int) -> str:
    seed_ms = max(from_ms - SEED_MINUTES * MINUTE_MS, 0)
    return f"""
    WITH t AS (
        SELECT symbol, ts_recv, ltp,
            volume - lag(volume) OVER w1 AS dv,
            last_value(nullif(bid_px_1, 0) IGNORE NULLS) OVER w0 AS b,
            last_value(nullif(ask_px_1, 0) IGNORE NULLS) OVER w0 AS a
        FROM read_parquet({_ql(path_glob)}, union_by_name=true)
        WHERE ts_recv >= {int(seed_ms)}
        WINDOW w0 AS (PARTITION BY symbol ORDER BY ts_recv
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
               w1 AS (PARTITION BY symbol ORDER BY ts_recv)
    )
    SELECT symbol, (ts_recv // {MINUTE_MS}) * {MINUTE_MS} AS m,
        sum(CASE WHEN a > b AND ltp >= a THEN dv ELSE 0 END) AS bought,
        sum(CASE WHEN a > b AND ltp <= b THEN dv ELSE 0 END) AS sold,
        sum(dv) AS traded,
        max(ts_recv) AS last_ms
    FROM t
    WHERE dv > 0 AND ts_recv >= {int(from_ms)}
    GROUP BY 1, 2
    """


def _read(path_glob: str, from_ms: int) -> tuple[dict[str, Minutes], int | None]:
    with _duck() as conn:
        conn.execute("SET threads=1")
        conn.execute("SET enable_progress_bar=false")
        rows = conn.execute(_sql(path_glob, from_ms)).fetchall()
    out: dict[str, Minutes] = {}
    newest: int | None = None
    for symbol, m, bought, sold, traded, last_ms in rows:
        bought, sold, traded = int(bought or 0), int(sold or 0), int(traded or 0)
        out.setdefault(symbol, {})[_minute(int(m))] = (bought, sold, max(traded - bought - sold, 0))
        newest = max(newest or 0, int(last_ms))
    return out, newest


def minute_trades(underlying: str, day: date) -> dict[str, Minutes] | None:
    """Each contract's (bought, sold, unplaced) volume per minute for one day, or None.

    None when the tick store holds nothing for that day and underlying, or the
    read failed (logged); Writer Flow then reads the premium as before.
    """
    path_glob = _partition_glob(day, underlying)
    if not glob.glob(path_glob):
        return None
    key = (underlying, day)
    with _lock:
        held = _days.get(key) or {"from_ms": 0, "minutes": {}}
        try:
            fresh, newest = _read(path_glob, held["from_ms"])
        except Exception:
            logger.exception("Writer Flow: reading trades for %s %s failed", underlying, day)
            return held["minutes"] or None
        cut = _minute(held["from_ms"]) if held["from_ms"] else None
        minutes = {
            sym: {m: v for m, v in series.items() if cut is None or m < cut}
            for sym, series in held["minutes"].items()
        }
        for sym, series in fresh.items():
            minutes.setdefault(sym, {}).update(series)
        if newest is not None:
            from_ms = (newest // MINUTE_MS - REREAD_MINUTES + 1) * MINUTE_MS
            held = {"from_ms": max(from_ms, held["from_ms"]), "minutes": minutes}
        else:
            held = {"from_ms": held["from_ms"], "minutes": minutes}
        _days[key] = held
        return held["minutes"] or None


def forget() -> None:
    """Drop everything read. Tests use it."""
    with _lock:
        _days.clear()
