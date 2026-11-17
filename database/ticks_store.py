"""Tick-level market-data store: a Parquet-per-day lake, queried with DuckDB.

The tick capture service (``services/tick_capture_service.py``) streams every
raw tick for a set of index + option symbols and hands batches to this module,
which writes them as compressed Parquet files partitioned by trade date and
underlying::

    db/ticks/date=2026-11-18/underlying=NIFTY/part-093015-0001.parquet
    db/ticks/date=2026-11-18/underlying=BANKNIFTY/compacted.parquet

Why a Parquet lake and not a single DuckDB file like ``oi_flow_db``: at full
5-level-depth tick volume (~0.5-1 GB/day) a single growing file cannot be kept
"forever" on one disk, and a VACUUM to reclaim space rewrites the whole file.
One file-set per day makes retention and off-box archival trivial (move or drop
one folder) and compression maximal. DuckDB reads across every day-file as one
table via ``read_parquet(..., hive_partitioning=true)`` with partition pruning
on the ``date`` column, and resamples ticks to OHLCV bars in SQL on read so the
existing ``source="db"`` backtest consumers work unchanged.

Only ONE process writes (the capture service), guarded by a file lock exactly
like ``oi_flow_db.claim_writer``. Reads open an in-memory DuckDB connection per
call (no lock on any file), so external read-only backtest processes can query
the same lake concurrently.

Design notes carried from the rest of the codebase:
  * No secondary indexes / sort beyond the daily compaction: Parquet row-group
    zone maps serve the range scans, mirroring the Historify #1779 lesson.
  * ``ts_exch`` is normalised to epoch MILLISECONDS on write regardless of what
    unit the broker sent, so bar bucketing is broker-agnostic.
"""

from __future__ import annotations

import glob as _glob
import os
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime

from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """Root of the Parquet lake. Read at call time so tests can repoint it."""
    raw = os.getenv("TICKS_DATA_DIR", "db/ticks").strip() or "db/ticks"
    return raw if os.path.isabs(raw) else os.path.join(_PROJECT_ROOT, raw)


def ensure_data_dir() -> str:
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _compression() -> str:
    return (os.getenv("TICKS_COMPRESSION", "zstd").strip() or "zstd").lower()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# ``date`` and ``underlying`` are hive partition keys encoded in the directory
# path, NOT stored in the file (DuckDB re-adds them as columns on read). Depth
# is flattened to fixed 5-level columns. ``*_ord_*`` (order counts) and the
# IV/Greeks columns are kept nullable for broker-generality and forward-compat:
# Upstox does not send order counts, and the adapter does not yet forward
# IV/Greeks, so those are written null today without a future schema change.

_DEPTH_LEVELS = 5

_BASE_COLUMNS = [
    "symbol",
    "exchange",
    "ts_exch",  # exchange tick time, epoch ms
    "ts_recv",  # local arrival time, epoch ms
    "ltp",
    "atp",
    "volume",
    "oi",
    "tbq",
    "tsq",
    "open",
    "high",
    "low",
    "close",
]
_DEPTH_COLUMNS = (
    [f"bid_px_{i}" for i in range(1, _DEPTH_LEVELS + 1)]
    + [f"bid_qty_{i}" for i in range(1, _DEPTH_LEVELS + 1)]
    + [f"bid_ord_{i}" for i in range(1, _DEPTH_LEVELS + 1)]
    + [f"ask_px_{i}" for i in range(1, _DEPTH_LEVELS + 1)]
    + [f"ask_qty_{i}" for i in range(1, _DEPTH_LEVELS + 1)]
    + [f"ask_ord_{i}" for i in range(1, _DEPTH_LEVELS + 1)]
)
_GREEK_COLUMNS = ["iv", "delta", "theta", "gamma", "vega", "rho"]

FILE_COLUMNS = _BASE_COLUMNS + _DEPTH_COLUMNS + _GREEK_COLUMNS


def _arrow_schema():
    import pyarrow as pa

    fields = [
        ("symbol", pa.string()),
        ("exchange", pa.string()),
        ("ts_exch", pa.int64()),
        ("ts_recv", pa.int64()),
        ("ltp", pa.float64()),
        ("atp", pa.float64()),
        ("volume", pa.int64()),
        ("oi", pa.int64()),
        ("tbq", pa.int64()),
        ("tsq", pa.int64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
    ]
    for name in _DEPTH_COLUMNS:
        fields.append((name, pa.int64() if ("_qty_" in name or "_ord_" in name) else pa.float64()))
    for name in _GREEK_COLUMNS:
        fields.append((name, pa.float64()))
    return pa.schema(fields)


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _int(value):
    f = _num(value)
    return int(f) if f is not None else None


def _norm_ms(value):
    """Normalise a broker timestamp to epoch milliseconds.

    Upstox sends ``ltt`` in ms; some brokers send seconds. Anything below
    1e12 (i.e. a plausible epoch in seconds, not ms) is scaled up, so bar
    bucketing is unit-agnostic across brokers.
    """
    v = _int(value)
    if v is None or v <= 0:
        return None
    return v * 1000 if v < 1_000_000_000_000 else v


def row_from_tick(
    symbol: str, exchange: str, underlying: str, data: dict, trade_date: date
) -> dict:
    """Build one flat store row from a normalised websocket tick ``data`` dict.

    ``data`` is the inner payload of a ``market_data`` frame (see
    ``broker/*/streaming`` adapters): ``ltp, atp(average_price), volume, oi,
    total_buy/sell_quantity, open/high/low/close, ltt, timestamp, depth``.
    Returns a dict keyed by every FILE_COLUMN plus ``trade_date`` / ``underlying``
    (used for partition routing, not written into the file).
    """
    row = dict.fromkeys(FILE_COLUMNS)
    row["trade_date"] = trade_date
    row["underlying"] = underlying
    row["symbol"] = symbol
    row["exchange"] = exchange
    row["ts_exch"] = _norm_ms(data.get("ltt") or data.get("last_trade_time"))
    row["ts_recv"] = _norm_ms(data.get("timestamp"))
    row["ltp"] = _num(data.get("ltp"))
    row["atp"] = _num(data.get("average_price"))
    row["volume"] = _int(data.get("volume"))
    row["oi"] = _int(data.get("oi"))
    row["tbq"] = _int(data.get("total_buy_quantity"))
    row["tsq"] = _int(data.get("total_sell_quantity"))
    row["open"] = _num(data.get("open"))
    row["high"] = _num(data.get("high"))
    row["low"] = _num(data.get("low"))
    row["close"] = _num(data.get("close"))

    depth = data.get("depth") or {}
    for side, px, qty in (("buy", "bid_px", "bid_qty"), ("sell", "ask_px", "ask_qty")):
        levels = depth.get(side) or []
        for i in range(_DEPTH_LEVELS):
            if i < len(levels):
                level = levels[i] or {}
                row[f"{px}_{i + 1}"] = _num(level.get("price"))
                row[f"{qty}_{i + 1}"] = _int(level.get("quantity"))
                # Order counts: present on some brokers (Angel), absent on Upstox.
                ord_name = ("bid_ord" if side == "buy" else "ask_ord") + f"_{i + 1}"
                row[ord_name] = _int(level.get("orders"))
    return row


# ---------------------------------------------------------------------------
# Single-writer file lock (mirrors oi_flow_db.claim_writer)
# ---------------------------------------------------------------------------

_writer_handle = None


def claim_writer() -> bool:
    """Take an exclusive, non-blocking lock so only one process writes the lake.

    DuckDB/Parquet writes here are plain file writes with no cross-process lock
    of their own, so two writers could interleave part-files. Held for the life
    of the process. Returns False if another process already holds it.
    """
    global _writer_handle
    if _writer_handle is not None:
        return True
    ensure_data_dir()
    lock_path = os.path.join(data_dir(), ".writer")
    try:
        handle = open(lock_path, "w")
        if os.name == "nt":  # pragma: no cover - Windows
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        handle.flush()
        _writer_handle = handle
        return True
    except Exception as exc:
        logger.warning("ticks_store: could not claim the writer lock: %s", exc)
        try:
            handle.close()
        except Exception:
            pass
        return False


def release_writer() -> None:
    global _writer_handle
    handle, _writer_handle = _writer_handle, None
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

_seq = 0


def _partition_dir(trade_date: date, underlying: str) -> str:
    return os.path.join(data_dir(), f"date={trade_date.isoformat()}", f"underlying={underlying}")


def write_batch(rows: list[dict]) -> int:
    """Write a batch of store rows (from ``row_from_tick``) to the lake.

    Groups rows by (trade_date, underlying) and writes one Parquet part-file per
    group per call. Returns the number of rows written. Never raises: a writer
    thread that raised would stop capture. The caller holds the writer lock
    (``claim_writer``) for the life of the process.
    """
    if not rows:
        return 0
    global _seq
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = _arrow_schema()
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row.get("trade_date"), row.get("underlying") or "UNKNOWN")
        groups.setdefault(key, []).append(row)

    written = 0
    for (trade_date, underlying), group in groups.items():
        if not isinstance(trade_date, date):
            continue
        part_dir = _partition_dir(trade_date, underlying)
        try:
            os.makedirs(part_dir, exist_ok=True)
            records = [{c: r.get(c) for c in FILE_COLUMNS} for r in group]
            table = pa.Table.from_pylist(records, schema=schema)
            _seq += 1
            stamp = datetime.now().strftime("%H%M%S")
            path = os.path.join(part_dir, f"part-{stamp}-{_seq:05d}.parquet")
            pq.write_table(table, path, compression=_compression())
            written += len(group)
        except Exception:
            logger.exception(
                "ticks_store: failed to write a part-file for %s/%s", trade_date, underlying
            )
    return written


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


@contextmanager
def _duck():
    """An in-memory DuckDB connection for reads. No file lock; closed in finally."""
    import duckdb

    conn = duckdb.connect()
    try:
        yield conn
    finally:
        conn.close()


def _lake_glob() -> str:
    return os.path.join(data_dir(), "date=*", "underlying=*", "*.parquet")


def _has_data() -> bool:
    return bool(_glob.glob(_lake_glob()))


def _ql(value: str) -> str:
    """Quote a string literal for safe inlining (DuckDB plans literals far better
    than bound params for partition/date filters; the values here are symbols
    and ISO dates, validated by the callers)."""
    return "'" + str(value).replace("'", "''") + "'"


def available_days() -> list[str]:
    """Trade dates present in the lake (ISO strings), ascending."""
    days = set()
    for path in _glob.glob(os.path.join(data_dir(), "date=*")):
        base = os.path.basename(path)
        if base.startswith("date="):
            days.add(base[len("date=") :])
    return sorted(days)


def query_ticks(
    symbols,
    start_date: str,
    end_date: str,
    start_ts_ms: int | None = None,
    end_ts_ms: int | None = None,
):
    """Raw ticks for ``symbols`` between two trade dates (ISO ``YYYY-MM-DD``).

    Returns a pandas DataFrame with FILE_COLUMNS plus ``date``/``underlying``.
    Partition-pruned on ``date``. ``symbols`` may be a string or a list.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = [s for s in (symbols or []) if s]
    if not symbols or not _has_data():
        import pandas as pd

        return pd.DataFrame(columns=FILE_COLUMNS + ["date", "underlying"])

    sym_list = ", ".join(_ql(s) for s in symbols)
    where = [
        f"date BETWEEN DATE {_ql(start_date)} AND DATE {_ql(end_date)}",
        f"symbol IN ({sym_list})",
    ]
    if start_ts_ms is not None:
        where.append(f"ts_exch >= {int(start_ts_ms)}")
    if end_ts_ms is not None:
        where.append(f"ts_exch <= {int(end_ts_ms)}")
    sql = (
        f"SELECT * FROM read_parquet({_ql(_lake_glob())}, hive_partitioning=true, "
        f"union_by_name=true) WHERE {' AND '.join(where)} ORDER BY symbol, ts_exch"
    )
    with _duck() as conn:
        return conn.execute(sql).df()


_INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "60m": 3600,
    "d": 86400,
    "1d": 86400,
    "day": 86400,
}


def _interval_seconds(interval: str) -> int:
    return _INTERVAL_SECONDS.get(str(interval).lower().strip(), 60)


def get_ohlcv(
    symbol: str,
    exchange: str,
    interval: str,
    start_ts: int | None = None,
    end_ts: int | None = None,
):
    """Resample stored ticks for one symbol to OHLCV bars, in DuckDB SQL.

    ``start_ts`` / ``end_ts`` are epoch SECONDS (like Historify). Returns a
    pandas DataFrame ``[timestamp, open, high, low, close, volume, oi]`` where
    ``timestamp`` is the bar-start epoch seconds — the same shape Historify's
    ``get_ohlcv`` returns, so ``source="db"`` consumers work unchanged.
    Bars are UTC-interval aligned (1-minute bars coincide with Historify's).
    """
    import pandas as pd

    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    if not symbol or not _has_data():
        return empty

    seconds = _interval_seconds(interval)
    # Partition pruning: derive the covered date range from the ts bounds.
    if start_ts is not None:
        start_date = datetime.fromtimestamp(start_ts, UTC).date().isoformat()
    else:
        days = available_days()
        start_date = days[0] if days else "1970-01-01"
    if end_ts is not None:
        end_date = datetime.fromtimestamp(end_ts, UTC).date().isoformat()
    else:
        days = available_days()
        end_date = days[-1] if days else "2999-12-31"

    where = [
        f"date BETWEEN DATE {_ql(start_date)} AND DATE {_ql(end_date)}",
        f"symbol = {_ql(symbol)}",
        f"exchange = {_ql(exchange)}",
        "ts_exch IS NOT NULL",
        "ltp IS NOT NULL",
    ]
    if start_ts is not None:
        where.append(f"ts_exch >= {int(start_ts) * 1000}")
    if end_ts is not None:
        where.append(f"ts_exch <= {int(end_ts) * 1000}")

    sql = f"""
        WITH t AS (
            SELECT ts_exch, ltp, volume, oi,
                   to_timestamp(ts_exch / 1000.0) AS tsutc
            FROM read_parquet({_ql(_lake_glob())}, hive_partitioning=true, union_by_name=true)
            WHERE {" AND ".join(where)}
        )
        SELECT
            CAST(epoch(time_bucket(INTERVAL '{seconds} seconds', tsutc)) AS BIGINT) AS timestamp,
            arg_min(ltp, ts_exch) AS open,
            max(ltp) AS high,
            min(ltp) AS low,
            arg_max(ltp, ts_exch) AS close,
            COALESCE(arg_max(volume, ts_exch) - arg_min(volume, ts_exch), 0) AS volume,
            COALESCE(arg_max(oi, ts_exch), 0) AS oi
        FROM t
        GROUP BY 1
        ORDER BY 1
    """
    with _duck() as conn:
        df = conn.execute(sql).df()
    if df.empty:
        return empty
    df["volume"] = df["volume"].clip(lower=0).astype("int64")
    df["oi"] = df["oi"].fillna(0).astype("int64")
    return df


# ---------------------------------------------------------------------------
# Daily compaction
# ---------------------------------------------------------------------------


def compact_day(trade_date: str) -> int:
    """Merge a day's many part-files into one sorted Parquet per underlying.

    Runs after the close. Reduces file count (faster reads) and re-sorts by
    (symbol, ts_exch) so row-group zone maps prune symbol scans. Idempotent:
    re-running folds any existing ``compacted.parquet`` back in. Returns the
    number of underlyings compacted.
    """
    day_dir = os.path.join(data_dir(), f"date={trade_date}")
    if not os.path.isdir(day_dir):
        return 0
    compacted = 0
    for under_dir in sorted(_glob.glob(os.path.join(day_dir, "underlying=*"))):
        parts = _glob.glob(os.path.join(under_dir, "*.parquet"))
        if len(parts) <= 1 and any(p.endswith("compacted.parquet") for p in parts):
            continue
        if not parts:
            continue
        tmp = os.path.join(under_dir, "compacted.tmp.parquet")
        src_glob = os.path.join(under_dir, "*.parquet")
        try:
            with _duck() as conn:
                conn.execute(
                    f"COPY (SELECT * FROM read_parquet({_ql(src_glob)}, union_by_name=true) "
                    f"ORDER BY symbol, ts_exch) TO {_ql(tmp)} "
                    f"(FORMAT PARQUET, COMPRESSION {_compression()})"
                )
            for old in parts:
                try:
                    os.remove(old)
                except OSError:
                    pass
            os.replace(tmp, os.path.join(under_dir, "compacted.parquet"))
            compacted += 1
        except Exception:
            logger.exception("ticks_store: compaction failed for %s", under_dir)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    return compacted


def data_stats() -> dict:
    """Lightweight footprint stats for diagnostics."""
    files = _glob.glob(_lake_glob())
    size = 0
    for f in files:
        try:
            size += os.path.getsize(f)
        except OSError:
            pass
    return {
        "days": len(available_days()),
        "files": len(files),
        "bytes": size,
        "mb": round(size / (1024 * 1024), 1),
        "dir": data_dir(),
    }
