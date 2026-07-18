# database/tf_boost_db.py
"""
TF Boost Snapshot DuckDB Database Module

Fully isolated from historify.duckdb — its own file, its own schema. Stores
periodic snapshots of TradeFinder's market_pulse ranked lists (intraday_boost,
breakout_beacon, high_powered_stocks) for future backtest replay.
"""

from __future__ import annotations
import os
import time
from contextlib import contextmanager

from utils.logging import get_logger

logger = get_logger(__name__)

TF_BOOST_DB_PATH = os.getenv("TF_BOOST_DATABASE_PATH", "db/tf_boost_snapshots.duckdb")


def get_db_path() -> str:
    """Get absolute path to the DuckDB database file."""
    if os.path.isabs(TF_BOOST_DB_PATH):
        return TF_BOOST_DB_PATH
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, TF_BOOST_DB_PATH)


def ensure_db_directory():
    """Ensure the database directory exists."""
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"Created database directory: {db_dir}")


@contextmanager
def get_connection(max_retries: int = 3, retry_delay: float = 0.5):
    """Get a DuckDB connection with retry logic (mirrors database/historify_db.py)."""
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
                logger.debug(f"DuckDB connection attempt {attempt + 1} failed, retrying: {e}")
                time.sleep(retry_delay * (attempt + 1))
            else:
                logger.exception(f"Failed to connect to DuckDB after {max_retries} attempts: {e}")

    if conn is None:
        raise last_error or Exception("Failed to connect to DuckDB")

    try:
        yield conn
    finally:
        conn.close()


def init_tf_boost_database():
    """Create the tf_boost_snapshots table (idempotent). Never touches historify.duckdb."""
    with get_connection() as conn:
        conn.execute("CREATE SEQUENCE IF NOT EXISTS tf_boost_snapshots_id_seq START 1")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tf_boost_snapshots (
                id BIGINT PRIMARY KEY DEFAULT nextval('tf_boost_snapshots_id_seq'),
                snapshot_date  DATE NOT NULL,
                snapshot_time  TIMESTAMP NOT NULL,
                list_type      VARCHAR NOT NULL,
                rank           INTEGER NOT NULL,
                symbol         VARCHAR NOT NULL,
                ltp            DOUBLE,
                prev_close     DOUBLE,
                change_pct     DOUBLE,
                score          DOUBLE,
                created_at     TIMESTAMP DEFAULT current_timestamp,
                UNIQUE (snapshot_time, list_type, symbol)
            )
        """)
    logger.info("TF Boost snapshot database initialized (isolated, no historify schema touched)")


def get_boost_symbols(
    start_date: str,
    end_date: str | None = None,
    list_type: str = "intraday_boost",
) -> list[str]:
    """Return the UNION of distinct symbols that appeared in `list_type` across
    the [start_date, end_date] window (inclusive, YYYY-MM-DD). This is the whole
    day's boost universe — including symbols that entered the list early and left
    before now — for point-in-time-honest backtesting. Returns [] if the table
    doesn't exist yet or has no rows for the window (never raises for the caller).
    """
    end_date = end_date or start_date
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM tf_boost_snapshots
                WHERE snapshot_date BETWEEN ? AND ?
                  AND list_type = ?
                ORDER BY symbol
                """,
                [start_date, end_date, list_type],
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning(f"get_boost_symbols({start_date}..{end_date}, {list_type}): {e}")
        return []
