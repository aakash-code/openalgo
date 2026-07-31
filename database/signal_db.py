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
import secrets
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class SignalSubscriber(Base):
    """A third-party platform authorised to consume the signal feed.

    Purpose-built rather than reusing the OAuth stack in database/oauth_db.py.
    That machinery — dynamic client registration, a browser consent round-trip,
    RS256 key lifecycle, refresh-token family rotation — exists to delegate a
    human's authority to an app. Subscribers here are server-to-server, each
    needing exactly one long-lived credential with exactly one permission. What
    IS borrowed is its shape: a hashed secret and an explicit revoked_at.

    Revisit this if subscriber count passes ~20 or per-subscriber scopes appear.
    """

    __tablename__ = "signal_subscribers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    # Argon2 hash of the bearer key — the plaintext is shown once at creation
    # and never stored, so a database leak does not yield working credentials.
    key_hash = Column(String(255), nullable=False)
    # Optional: set to receive push. Unset means pull-only via /events.
    webhook_url = Column(String(500), nullable=True)
    # Fernet-encrypted; the subscriber verifies our HMAC signature with it.
    hmac_secret_enc = Column(String(500), nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True)


def init_db() -> None:
    """Create the signal tables."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Signal DB", logger)


def create_subscriber(name: str, webhook_url: str | None = None) -> tuple[str, str]:
    """Create a subscriber. Returns (api_key, hmac_secret) in PLAINTEXT, once.

    Neither value is recoverable afterwards — the key is stored only as an
    argon2 hash. Re-issuing means creating a new subscriber.
    """
    from database.auth_db import PEPPER, encrypt_token, ph

    api_key = secrets.token_urlsafe(32)
    hmac_secret = secrets.token_urlsafe(32)

    row = SignalSubscriber(
        name=name,
        key_hash=ph.hash(api_key + PEPPER),
        webhook_url=webhook_url,
        hmac_secret_enc=encrypt_token(hmac_secret),
        active=True,
    )
    db_session.add(row)
    db_session.commit()
    return api_key, hmac_secret


def verify_subscriber(api_key: str) -> SignalSubscriber | None:
    """Return the active subscriber holding this key, or None.

    Mirrors auth_db.verify_api_key: argon2 has a per-row salt, so there is no
    hash to look up by — every active row must be checked.

    ponytail: O(subscribers) argon2 verifies per request, ~50ms each. Fine for a
    handful of B2B consumers polling a backfill endpoint. If subscriber count
    grows, add a sha256(pepper+key) lookup column and index it.
    """
    from database.auth_db import PEPPER, ph

    if not api_key:
        return None

    for row in db_session.query(SignalSubscriber).filter(
        SignalSubscriber.active.is_(True), SignalSubscriber.revoked_at.is_(None)
    ):
        try:
            if ph.verify(row.key_hash, api_key + PEPPER):
                return row
        except Exception:
            # argon2 raises VerifyMismatchError for a wrong key and other
            # subclasses for a malformed hash; neither should stop the scan.
            continue
    return None


def decrypt_hmac_secret(subscriber: SignalSubscriber) -> str | None:
    """Plaintext HMAC secret for signing this subscriber's webhooks."""
    from database.auth_db import decrypt_token

    if not subscriber.hmac_secret_enc:
        return None
    try:
        return decrypt_token(subscriber.hmac_secret_enc)
    except Exception:
        logger.exception(f"Could not decrypt HMAC secret for subscriber {subscriber.id}")
        return None


def get_webhook_subscribers() -> list[SignalSubscriber]:
    """Active subscribers with a webhook URL configured."""
    return (
        db_session.query(SignalSubscriber)
        .filter(
            SignalSubscriber.active.is_(True),
            SignalSubscriber.revoked_at.is_(None),
            SignalSubscriber.webhook_url.isnot(None),
        )
        .all()
    )


def revoke_subscriber(subscriber_id: int) -> bool:
    """Revoke access immediately. Returns False if no such subscriber."""
    row = db_session.get(SignalSubscriber, subscriber_id)
    if row is None:
        return False
    row.active = False
    row.revoked_at = datetime.utcnow()
    db_session.commit()
    return True


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
