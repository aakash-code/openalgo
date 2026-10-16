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


def load_day(underlying: str, expiry: str, trade_date: date) -> list[dict]:
    """Every sample for one underlying and expiry on one day, oldest first."""
    where = _day_filter(underlying, expiry, trade_date)
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            SELECT {", ".join(SAMPLE_COLUMNS)} FROM oi_flow_samples
            WHERE {where}
            ORDER BY sample_time, strike, side
            """
        )
        return [dict(zip(SAMPLE_COLUMNS, row, strict=True)) for row in cur.fetchall()]


def recorded_minutes(underlying: str, expiry: str, trade_date: date) -> set[datetime]:
    """The minutes already held for a day, so a backfill can ask only for the gaps."""
    where = _day_filter(underlying, expiry, trade_date)
    with get_connection() as conn:
        cur = conn.execute(f"SELECT DISTINCT sample_time FROM oi_flow_samples WHERE {where}")
        return {row[0] for row in cur.fetchall()}


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
