# database/signal_db.py
"""Storage for broadcast trading signals.

Lives on the main SQLAlchemy database rather than its own DuckDB file (as
database/tf_boost_db.py uses). DuckDB fits tf_boost's workload — bulk snapshot
writes replayed offline for backtests. This is the opposite shape: a few hundred
small rows a day, written one at a time as signals fire, read concurrently by
Flask workers serving subscriber range queries. DuckDB's single-writer model
would be a hazard there for no benefit.

What IS borrowed from tf_boost_db is the idempotency pattern: a UNIQUE natural
key plus insert-and-ignore-conflict. That, rather than producer discipline, is
what makes a retried POST or a briefly duplicated producer harmless.
"""

import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    create_engine,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
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


class SignalEvent(Base):
    """One broadcastable edge: a signal opening, or ceasing to be valid.

    Deliberately does NOT store quantity — subscribers size positions against
    their own capital — nor any upstream-provider scoring or vocabulary.
    """

    __tablename__ = "signal_events"

    # Autoincrement id doubles as the subscriber backfill cursor: monotonic and
    # immune to clock skew, unlike paging on a timestamp.
    id = Column(Integer, primary_key=True, autoincrement=True)
    # f"{symbol}:{signal_ts}:{event}" — the idempotency key.
    event_id = Column(String(128), nullable=False, unique=True, index=True)
    event = Column(String(16), nullable=False)  # open | invalidated
    symbol = Column(String(64), nullable=False)
    exchange = Column(String(16), nullable=False)
    sector = Column(String(64), nullable=False)
    side = Column(String(8), nullable=False)  # LONG | SHORT
    entry = Column(Float, nullable=False)
    stop = Column(Float, nullable=False)
    target = Column(Float, nullable=False)
    stop_pct = Column(Float, nullable=False)
    # ISO-8601 with a real +05:30 offset, as produced by the producer.
    signal_time_ist = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("ix_signal_events_created_at", "created_at"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "eventId": self.event_id,
            "event": self.event,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "sector": self.sector,
            "side": self.side,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "stopPct": self.stop_pct,
            "signalTimeIst": self.signal_time_ist,
        }


def init_db() -> None:
    """Create the signal tables."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Signal DB", logger)


def insert_signal_event(payload: dict) -> SignalEvent | None:
    """Insert one event. Returns the row, or None if `event_id` already existed.

    A None return is the caller's signal to skip fan-out — that is what stops a
    retried POST from double-posting to Telegram.
    """
    row = SignalEvent(
        event_id=payload["eventId"],
        event=payload["event"],
        symbol=payload["symbol"],
        exchange=payload["exchange"],
        sector=payload["sector"],
        side=payload["side"],
        entry=payload["entry"],
        stop=payload["stop"],
        target=payload["target"],
        stop_pct=payload["stopPct"],
        signal_time_ist=payload["signalTimeIst"],
    )
    try:
        db_session.add(row)
        db_session.commit()
        return row
    except IntegrityError:
        # Duplicate event_id — the expected path for a retry, not an error.
        db_session.rollback()
        return None
    except Exception:
        db_session.rollback()
        raise


def get_active_signals(ist_date: str) -> list[SignalEvent]:
    """Signals still open right now, for the grouped channel summary.

    Derived from the event log rather than tracked separately: the newest event
    per symbol on `ist_date` wins, and the symbol is active only if that event
    is an `open`. So an open followed by an invalidation drops out, and a
    re-entry after an invalidation comes back — with no extra state to keep in
    sync, and correct immediately after a restart.

    Scoped to one IST day because this strategy time-exits every position by
    15:10; there is no legitimate cross-day active signal.
    """
    latest_per_symbol = (
        select(func.max(SignalEvent.id))
        .where(SignalEvent.signal_time_ist.like(f"{ist_date}%"))
        .group_by(SignalEvent.symbol)
    )
    return (
        db_session.query(SignalEvent)
        .filter(SignalEvent.id.in_(latest_per_symbol), SignalEvent.event == "open")
        .order_by(SignalEvent.sector.asc(), SignalEvent.symbol.asc())
        .all()
    )


def get_events_since(since_id: int = 0, limit: int = 200) -> list[SignalEvent]:
    """Events with id > since_id, oldest first. The subscriber backfill query."""
    return (
        db_session.query(SignalEvent)
        .filter(SignalEvent.id > since_id)
        .order_by(SignalEvent.id.asc())
        .limit(max(1, min(int(limit), 1000)))
        .all()
    )
