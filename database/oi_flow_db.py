# database/oi_flow_db.py
"""
OI Flow DuckDB Database Module

The per-minute record behind Writer Flow: the open interest and premium of
every option contract around ATM, sampled through the day, so the analysis can
say where writers have been adding, covering or holding and for how long.

Fully isolated: its own file (db/oi_flow.duckdb), its own schema, nothing
shared with openalgo.db, historify.duckdb or the OI Profile. A connection is
opened per operation and always closed (see the FD rules in CLAUDE.md).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from utils import real_threading
from utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_DB_PATH = "db/oi_flow.duckdb"

# What an underlying and an expiry may look like. The day filter is written into
# the SQL as literals (see _day_filter), so these are the guard, not a nicety.
UNDERLYING_RE = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
EXPIRY_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}$")


# How many writes each underlying has had in this process. Writer Flow keeps
# its answers until this moves, so a refresh with nothing new costs nothing.
# Exact, because only this process can open the file (DuckDB's lock), and the
# recorder writes through insert_samples. A real lock: the section is dict
# work only, and both a green and a real thread may write.
_generations: dict[str, int] = {}
_generations_lock = real_threading.Lock()


def generation(underlying: str) -> int:
    """The write count for an underlying; changes after every stored write."""
    return _generations.get(underlying, 0)


def _bump_generations(underlyings: set[str]) -> None:
    with _generations_lock:
        for u in underlyings:
            _generations[u] = _generations.get(u, 0) + 1


# The one process allowed to write the record, held for that process's life.
_writer_handle = None


def claim_writer() -> bool:
    """Become the only process that writes the record; False if another holds it.

    DuckDB locks the file only while a connection is open, and this module opens
    one per operation, so two processes can each write between the other's
    operations. On 16-Oct-2026 an OpenAlgo process still shutting down kept
    filling gaps with the code it had started with, and its rows landed in the
    record of the process that replaced it. A lock on a file beside the record,
    held until the process exits (the OS releases it even on a kill), leaves one
    writer.
    """
    global _writer_handle
    if _writer_handle is not None:
        return True
    ensure_db_directory()
    handle = open(get_db_path() + ".writer", "a+")  # noqa: SIM115 - held for the process's life
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _writer_handle = handle
    return True


def release_writer() -> None:
    """Give up the writer claim (shutdown)."""
    global _writer_handle
    if _writer_handle is not None:
        _writer_handle.close()
        _writer_handle = None


def get_db_path() -> str:
    """Absolute path to the DuckDB file. Read at call time so tests can point it elsewhere."""
    path = os.getenv("OI_FLOW_DATABASE_PATH", DEFAULT_DB_PATH)
    if os.path.isabs(path):
        return path
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, path)


def ensure_db_directory() -> None:
    db_dir = os.path.dirname(get_db_path())
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"Created database directory: {db_dir}")


@contextmanager
def get_connection(max_retries: int = 3, retry_delay: float = 0.5):
    """A DuckDB connection, closed on every exit path (mirrors tf_boost_db)."""
    ensure_db_directory()
    db_path = get_db_path()
    conn = None
    last_error = None
    for attempt in range(max_retries):
        try:
            import duckdb

            conn = duckdb.connect(db_path)
            break
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                logger.debug(f"OI Flow DuckDB connect attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(retry_delay * (attempt + 1))
            else:
                logger.exception(
                    f"Could not open the OI Flow database after {max_retries} attempts: {e}"
                )
    if conn is None:
        raise last_error or Exception("Could not open the OI Flow database")
    try:
        yield conn
    finally:
        conn.close()


_SETTINGS_TABLE = """
    CREATE TABLE IF NOT EXISTS oi_flow_settings (
        key    VARCHAR PRIMARY KEY,
        value  VARCHAR NOT NULL
    )
"""


def init_oi_flow_database() -> None:
    """Create the samples table (idempotent)."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oi_flow_samples (
                trade_date   DATE NOT NULL,
                sample_time  TIMESTAMP NOT NULL,
                underlying   VARCHAR NOT NULL,
                exchange     VARCHAR NOT NULL,
                expiry       VARCHAR NOT NULL,
                strike       DOUBLE NOT NULL,
                side         VARCHAR NOT NULL,
                symbol       VARCHAR NOT NULL,
                oi           BIGINT,
                ltp          DOUBLE,
                volume       BIGINT,
                spot         DOUBLE,
                lot_size     INTEGER,
                source       VARCHAR NOT NULL,
                PRIMARY KEY (symbol, sample_time)
            )
        """)
        # An index on (underlying, expiry, trade_date) was measured to change
        # nothing for load_day (1.94 s against 1.97 s) while costing every
        # insert, so a database created before that is relieved of it.
        conn.execute("DROP INDEX IF EXISTS oi_flow_day")
        # The market read (services/market_read_service.py): the futures and
        # India VIX it quotes each minute, the events it raised, its reading of
        # every minute, and its switches. Created here because they live in
        # this file and nothing else; an existing record simply gains them.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_read_quotes (
                trade_date   DATE NOT NULL,
                sample_time  TIMESTAMP NOT NULL,
                symbol       VARCHAR NOT NULL,
                exchange     VARCHAR NOT NULL,
                ltp          DOUBLE,
                oi           BIGINT,
                volume       BIGINT,
                prev_close   DOUBLE,
                source       VARCHAR NOT NULL,
                PRIMARY KEY (symbol, sample_time)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_read_events (
                trade_date   DATE NOT NULL,
                underlying   VARCHAR NOT NULL,
                id           VARCHAR NOT NULL,
                event_time   TIMESTAMP NOT NULL,
                kind         VARCHAR NOT NULL,
                level        VARCHAR NOT NULL,
                title        VARCHAR NOT NULL,
                reason       VARCHAR NOT NULL,
                PRIMARY KEY (trade_date, underlying, id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_read_minutes (
                trade_date   DATE NOT NULL,
                underlying   VARCHAR NOT NULL,
                sample_time  TIMESTAMP NOT NULL,
                state        VARCHAR NOT NULL,
                PRIMARY KEY (underlying, sample_time)
            )
        """)
        conn.execute(_SETTINGS_TABLE)


SAMPLE_COLUMNS = (
    "trade_date",
    "sample_time",
    "underlying",
    "exchange",
    "expiry",
    "strike",
    "side",
    "symbol",
    "oi",
    "ltp",
    "volume",
    "spot",
    "lot_size",
    "source",
)


def insert_samples(rows: list[dict]) -> int:
    """Store samples; a (symbol, minute) already held is left as it was.

    That is what lets a backfill run over minutes the live recorder already
    caught, and a restarted recorder repeat a minute, without duplicates or
    one source overwriting the other. Returns the number of rows offered.
    """
    if not rows:
        return 0
    values = [tuple(r.get(c) for c in SAMPLE_COLUMNS) for r in rows]
    placeholders = ", ".join("?" for _ in SAMPLE_COLUMNS)
    with get_connection() as conn:
        # One transaction, so a reader never sees half a minute. Committed row
        # by row, the Writer Flow chart asking mid-write read most strikes as
        # out of the window and the last step's totals as a fraction of the move.
        conn.begin()
        try:
            conn.executemany(
                f"INSERT OR IGNORE INTO oi_flow_samples ({', '.join(SAMPLE_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # After the commit, so an answer built from what a reader saw is never
    # stamped with a count that includes rows it could not see.
    _bump_generations({r.get("underlying") for r in rows if r.get("underlying")})
    return len(values)


def _day_filter(underlying: str, expiry: str, trade_date: date) -> str:
    """The WHERE clause for one underlying, expiry and day, as literals.

    Bound parameters made DuckDB plan this filter badly: on one morning's 30K
    rows load_day took 1.2 s with `?` (even with only underlying and expiry
    bound) and 40 ms with literals. Each value is checked against its shape
    first, and anything else is refused, so nothing from a request reaches the
    SQL unchecked.
    """
    if not UNDERLYING_RE.match(underlying or ""):
        raise ValueError(f"Not an underlying: {underlying!r}")
    if not EXPIRY_RE.match(expiry or ""):
        raise ValueError(f"Not an expiry: {expiry!r}")
    if not isinstance(trade_date, date):
        raise ValueError(f"Not a date: {trade_date!r}")
    day = trade_date.date() if isinstance(trade_date, datetime) else trade_date
    return (
        f"underlying = '{underlying}' AND expiry = '{expiry}' "
        f"AND trade_date = DATE '{day.isoformat()}'"
    )


# Rows read per batch when load_day is given a pause hook.
LOAD_BATCH = 2000


def load_day(
    underlying: str, expiry: str, trade_date: date, pause: Callable[[], None] | None = None
) -> list[dict]:
    """Every sample for one underlying and expiry on one day, oldest first.

    With `pause`, rows are read LOAD_BATCH at a time and `pause` is called
    between batches. A whole day (38,000 rows for CRUDEOIL by evening) read in
    one call held the interpreter for its length: request-shaped work on other
    threads went from 0.3 ms to 11 ms while it ran.
    """
    where = _day_filter(underlying, expiry, trade_date)
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            SELECT {", ".join(SAMPLE_COLUMNS)} FROM oi_flow_samples
            WHERE {where}
            ORDER BY sample_time, strike, side
            """
        )
        if pause is None:
            return [dict(zip(SAMPLE_COLUMNS, row, strict=True)) for row in cur.fetchall()]
        out: list[dict] = []
        while batch := cur.fetchmany(LOAD_BATCH):
            out.extend(dict(zip(SAMPLE_COLUMNS, row, strict=True)) for row in batch)
            pause()
        return out


# A minute holding fewer contracts than this share of the day's typical minute
# is not held: it is a fill that stopped partway, and it is asked for again.
COMPLETE_MINUTE_SHARE = 0.8


def recorded_minutes(underlying: str, expiry: str, trade_date: date) -> set[datetime]:
    """The minutes held whole for a day, so a backfill can ask only for the gaps.

    Whole, not merely present. On 19-Oct-2026 a fill failed partway and left
    sixteen NIFTY minutes with 12 of 31 strikes; counted as held because they
    had any row, they were never asked for again, and most strips on the chart
    stayed blank through them. A minute counts once it holds at least
    COMPLETE_MINUTE_SHARE of the day's median contracts per minute.
    """
    where = _day_filter(underlying, expiry, trade_date)
    with get_connection() as conn:
        cur = conn.execute(
            f"SELECT sample_time, COUNT(*) FROM oi_flow_samples WHERE {where} GROUP BY sample_time"
        )
        counts = dict(cur.fetchall())
    if not counts:
        return set()
    ordered = sorted(counts.values())
    typical = ordered[len(ordered) // 2]
    return {t for t, n in counts.items() if n >= typical * COMPLETE_MINUTE_SHARE}


def contracts_between(
    underlying: str, expiry: str, trade_date: date, start: datetime, end: datetime
) -> list[dict]:
    """The contracts recorded between two times on a day: the window as it was.

    A gap is filled for the strikes that were near the money around it, not
    for wherever ATM sits now. Times are datetimes, never request input, and go
    into the SQL as literals for the same reason _day_filter's values do.
    """
    where = _day_filter(underlying, expiry, trade_date)
    if not (isinstance(start, datetime) and isinstance(end, datetime)):
        raise ValueError("start and end must be datetimes")
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            SELECT DISTINCT symbol, strike, side, exchange, lot_size FROM oi_flow_samples
            WHERE {where}
              AND sample_time BETWEEN TIMESTAMP '{start.isoformat(sep=" ")}'
                                  AND TIMESTAMP '{end.isoformat(sep=" ")}'
            ORDER BY strike, side
            """
        )
        return [
            {"symbol": r[0], "strike": r[1], "side": r[2], "exchange": r[3], "lot_size": r[4]}
            for r in cur.fetchall()
        ]


def available_sessions(limit_days: int = 10) -> list[dict]:
    """Underlying, expiry and day combinations held, newest day first.

    The cutoff goes into the SQL as a date literal, not a bound parameter.
    Measured on one morning's 30K rows: 1.6 to 6.8 seconds with `?`, 12 ms
    with the literal, because DuckDB plans the parameterised filter far worse.
    Writer Flow asks for this on every refresh. The cutoff is a date this
    function derives from the table itself, never request input, so the
    literal carries no injection risk.
    """
    with get_connection() as conn:
        latest = conn.execute("SELECT MAX(trade_date) FROM oi_flow_samples").fetchone()[0]
        if latest is None:
            return []
        cutoff = latest - timedelta(days=limit_days)
        cur = conn.execute(
            f"""
            SELECT trade_date, underlying, expiry, COUNT(DISTINCT sample_time) AS minutes,
                   MAX(sample_time) AS last_sample
            FROM oi_flow_samples
            WHERE trade_date >= DATE '{cutoff.isoformat()}'
            GROUP BY trade_date, underlying, expiry
            ORDER BY trade_date DESC, underlying, expiry
            """
        )
        return [
            {
                "trade_date": r[0],
                "underlying": r[1],
                "expiry": r[2],
                "minutes": r[3],
                "last_sample": r[4],
            }
            for r in cur.fetchall()
        ]


def expiry_for_day(underlying: str, trade_date: date) -> str | None:
    """The expiry an underlying was recorded on for a day: the one with most rows."""
    if not UNDERLYING_RE.match(underlying or ""):
        raise ValueError(f"Not an underlying: {underlying!r}")
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT expiry FROM oi_flow_samples
            WHERE underlying = '{underlying}' AND trade_date = DATE '{trade_date.isoformat()}'
            GROUP BY expiry ORDER BY COUNT(*) DESC LIMIT 1
            """
        ).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------ market read

QUOTE_COLUMNS = (
    "trade_date",
    "sample_time",
    "symbol",
    "exchange",
    "ltp",
    "oi",
    "volume",
    "prev_close",
    "source",
)


def insert_market_quotes(rows: list[dict]) -> int:
    """Store the market read's quotes; a (symbol, minute) already held is kept."""
    if not rows:
        return 0
    values = [tuple(r.get(c) for c in QUOTE_COLUMNS) for r in rows]
    placeholders = ", ".join("?" for _ in QUOTE_COLUMNS)
    with get_connection() as conn:
        conn.executemany(
            f"INSERT OR IGNORE INTO market_read_quotes ({', '.join(QUOTE_COLUMNS)}) "
            f"VALUES ({placeholders})",
            values,
        )
    return len(values)


def load_market_quotes(trade_date: date) -> list[dict]:
    """Every quote the market read stored for a day, oldest first."""
    with get_connection() as conn:
        cur = conn.execute(
            f"SELECT {', '.join(QUOTE_COLUMNS)} FROM market_read_quotes "
            "WHERE trade_date = ? ORDER BY sample_time, symbol",
            [trade_date],
        )
        return [dict(zip(QUOTE_COLUMNS, row, strict=True)) for row in cur.fetchall()]


# An event within this many minutes of a held one of the same kind and strike
# is that event again, moved: a minute filled in from history later changes the
# minutes around it, and a turn read at 09:55 can read at 09:54 on the next pass.
SAME_EVENT_MINUTES = 5


def _stem(event_id: str) -> str:
    return event_id.rsplit(":", 1)[0]  # kind:key, without the minute


def store_read_events(underlying: str, trade_date: date, events: list[dict]) -> list[dict]:
    """Store events not already held, and return just those: the ones that are new."""
    if not events:
        return []
    with get_connection() as conn:
        held = conn.execute(
            "SELECT id, event_time FROM market_read_events WHERE trade_date = ? AND underlying = ?",
            [trade_date, underlying],
        ).fetchall()
        window = timedelta(minutes=SAME_EVENT_MINUTES)
        by_stem: dict[str, list[datetime]] = {}
        for event_id, at in held:
            by_stem.setdefault(_stem(event_id), []).append(at)
        new = []
        for e in events:
            times = by_stem.setdefault(_stem(e["id"]), [])
            if any(abs(e["time"] - at) <= window for at in times):
                continue
            times.append(e["time"])
            new.append(e)
        if new:
            conn.executemany(
                "INSERT OR IGNORE INTO market_read_events "
                "(trade_date, underlying, id, event_time, kind, level, title, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        trade_date,
                        underlying,
                        e["id"],
                        e["time"],
                        e["kind"],
                        e["level"],
                        e["title"],
                        e["reason"],
                    )
                    for e in new
                ],
            )
    return new


def store_read_minute(underlying: str, trade_date: date, sample_time: datetime, state: str) -> None:
    """Keep one minute's reading, replacing an earlier reading of the same minute."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO market_read_minutes "
            "(trade_date, underlying, sample_time, state) VALUES (?, ?, ?, ?)",
            [trade_date, underlying, sample_time, state],
        )


def read_setting(key: str) -> str | None:
    """A stored switch, or None when it was never set (or there is no record yet)."""
    if not os.path.exists(get_db_path()):
        return None
    with get_connection() as conn:
        conn.execute(_SETTINGS_TABLE)
        row = conn.execute("SELECT value FROM oi_flow_settings WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def write_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(_SETTINGS_TABLE)
        conn.execute(
            "INSERT OR REPLACE INTO oi_flow_settings (key, value) VALUES (?, ?)", [key, value]
        )
