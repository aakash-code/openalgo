# database/index_constituents_db.py
"""
Index Constituents DB
Stores NSE sectoral index membership + weights for the Sector Contribution /
Index Driver Engine. Versioned (effective_from/effective_to) so a bad monthly
sync can be rolled back with a single UPDATE.
"""

import os
from datetime import date, datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class IndexConstituent(Base):
    __tablename__ = "index_constituents"

    id = Column(Integer, primary_key=True)
    index_symbol = Column(String(50), nullable=False, index=True)  # e.g. 'BANKNIFTY'
    index_exchange = Column(String(20), nullable=False, default="NSE_INDEX")
    symbol = Column(String(50), nullable=False, index=True)  # e.g. 'HDFCBANK'
    exchange = Column(String(20), nullable=False, default="NSE")
    weight = Column(Float, nullable=True)  # percent, 0..100. NULL = membership known, weight unknown
    weight_source = Column(String(20), nullable=False, default="factsheet")  # factsheet|manual|equal
    effective_from = Column(Date, nullable=False, index=True)
    effective_to = Column(Date, nullable=True, index=True)  # NULL = current row
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_ic_index_active", "index_symbol", "effective_to"),
        UniqueConstraint("index_symbol", "symbol", "effective_from", name="uq_ic_version"),
    )


class IndexSyncStatus(Base):
    __tablename__ = "index_sync_status"

    id = Column(Integer, primary_key=True, default=1)
    status = Column(String(20), default="pending")  # pending|running|success|error
    message = Column(Text, nullable=True)
    last_updated = Column(DateTime, nullable=True)
    index_stats = Column(Text, nullable=True)  # JSON: {"BANKNIFTY": {"n": 12, "weighted": 100.0}, ...}
    weights_as_of = Column(Date, nullable=True)


def init_db():
    """Initialize the index constituents database"""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Index Constituents DB", logger)


def ensure_index_constituent_tables_exists():
    """Ensure tables exist (alias for init_db to match app.py pattern)"""
    init_db()


def get_current_constituents(index_symbol: str | None = None):
    """
    Return current (effective_to IS NULL) constituent rows, optionally filtered
    to one index. Ordered by index_symbol then weight descending.
    """
    try:
        query = IndexConstituent.query.filter_by(effective_to=None)
        if index_symbol:
            query = query.filter_by(index_symbol=index_symbol)
        rows = query.all()
        rows.sort(key=lambda r: (r.index_symbol, -(r.weight or 0)))
        return rows
    except Exception:
        logger.exception("[IndexConstituentsDB] Error fetching current constituents")
        return []


def get_supported_indices():
    """Distinct list of index_symbol values with a current (non-expired) version."""
    try:
        rows = (
            db_session.query(IndexConstituent.index_symbol, IndexConstituent.index_exchange)
            .filter(IndexConstituent.effective_to.is_(None))
            .distinct()
            .all()
        )
        return [{"index_symbol": r[0], "index_exchange": r[1]} for r in rows]
    except Exception:
        logger.exception("[IndexConstituentsDB] Error fetching supported indices")
        return []


def replace_index_version(index_symbol: str, index_exchange: str, constituents: list[dict],
                           effective_from: date | None = None, weight_source: str = "factsheet",
                           commit: bool = True):
    """
    Close out the current version of `index_symbol` (if any) and insert a new
    version. `constituents` is a list of {symbol, exchange, weight, weight_source?}.
    Only writes a new version if membership or weights actually differ from the
    current one, so unrelated indices are never touched and history stays clean.

    `commit=False` stages the change without committing/rolling back the
    session, so a caller writing several indices in one sync pass can batch
    them into a single transaction (one write-lock acquisition against
    openalgo.db instead of one per index) -- see sync_index_constituents(),
    which hit real "database is locked" contention during app boot when this
    was 11 separate commits fired back-to-back against the shared SQLite file.
    """
    effective_from = effective_from or date.today()
    try:
        current = IndexConstituent.query.filter_by(
            index_symbol=index_symbol, effective_to=None
        ).all()
        current_shape = sorted(
            (c.symbol, c.exchange, round(c.weight or 0, 4)) for c in current
        )
        new_shape = sorted(
            (c["symbol"], c.get("exchange", "NSE"), round(c.get("weight") or 0, 4))
            for c in constituents
        )
        if current_shape == new_shape and current:
            logger.debug(f"[IndexConstituentsDB] {index_symbol}: no change, skipping version bump")
            return False

        for row in current:
            row.effective_to = effective_from

        for c in constituents:
            db_session.add(
                IndexConstituent(
                    index_symbol=index_symbol,
                    index_exchange=index_exchange,
                    symbol=c["symbol"],
                    exchange=c.get("exchange", "NSE"),
                    weight=c.get("weight"),
                    weight_source=c.get("weight_source", weight_source),
                    effective_from=effective_from,
                    effective_to=None,
                )
            )
        if commit:
            db_session.commit()
        logger.info(
            f"[IndexConstituentsDB] {index_symbol}: staged new version with "
            f"{len(constituents)} constituents effective {effective_from}"
            + ("" if commit else " (uncommitted, batched)")
        )
        return True
    except Exception:
        logger.exception(f"[IndexConstituentsDB] Error replacing version for {index_symbol}")
        if commit:
            db_session.rollback()
            return False
        raise  # batched callers (commit=False) must know a member of the batch failed


def get_sync_status():
    try:
        status = IndexSyncStatus.query.get(1)
        if not status:
            return None
        return {
            "status": status.status,
            "message": status.message,
            "last_updated": status.last_updated.isoformat() if status.last_updated else None,
            "index_stats": status.index_stats,
            "weights_as_of": status.weights_as_of.isoformat() if status.weights_as_of else None,
        }
    except Exception:
        logger.exception("[IndexConstituentsDB] Error fetching sync status")
        return None


def set_sync_status(status: str, message: str | None = None, index_stats: str | None = None,
                     weights_as_of: date | None = None):
    try:
        row = IndexSyncStatus.query.get(1)
        if not row:
            row = IndexSyncStatus(id=1)
            db_session.add(row)
        row.status = status
        row.message = message
        row.last_updated = datetime.utcnow()
        if index_stats is not None:
            row.index_stats = index_stats
        if weights_as_of is not None:
            row.weights_as_of = weights_as_of
        db_session.commit()
    except Exception:
        logger.exception("[IndexConstituentsDB] Error setting sync status")
        db_session.rollback()
