"""Storage for the owner passcode (fork feature, see utils/owner_lock.py).

Kept in its own module and table so no upstream file changes: merging
upstream/main never conflicts here. Hashed with Argon2 and the same pepper as
login passwords.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Column, Integer, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.auth_db import PEPPER
from database.engine_factory import create_db_engine
from utils.logging import get_logger
from utils.thread_safe_cache import MISSING, LockedTTLCache

logger = get_logger(__name__)

engine = create_db_engine()
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

_ph = PasswordHasher()
_cache = LockedTTLCache(maxsize=2, ttl=3600)
_KEY = "hash"

#: Returned by get_passcode_hash() when the table cannot be read. It is not a
#: valid Argon2 hash, so no passcode verifies against it, and it is truthy, so
#: the lock reads as set: a read failure keeps the owner surface locked instead
#: of opening it.
UNREADABLE = "!unreadable"


class OwnerLock(Base):
    __tablename__ = "owner_lock"
    id = Column(Integer, primary_key=True)
    passcode_hash = Column(Text, nullable=True)


def init_db():
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Owner Lock DB", logger)


def get_passcode_hash() -> str | None:
    cached = _cache.get(_KEY, MISSING)
    if cached is not MISSING:
        return cached
    generation = _cache.generation
    try:
        row = OwnerLock.query.first()
        value = row.passcode_hash if row and row.passcode_hash else None
    except Exception:
        db_session.rollback()
        logger.exception("Owner lock table could not be read; keeping the owner lock closed")
        return UNREADABLE  # not cached, so the next request reads again
    finally:
        db_session.remove()
    _cache.fill(_KEY, value, generation)
    return value


def is_passcode_set() -> bool:
    return get_passcode_hash() is not None


def verify_passcode(code: str) -> bool:
    stored = get_passcode_hash()
    if not stored or not code:
        return False
    if stored == UNREADABLE:
        return False
    try:
        return _ph.verify(stored, code + PEPPER)
    except (VerificationError, InvalidHashError):
        return False


def set_passcode(code: str | None) -> None:
    """Store a new passcode, or remove it when ``code`` is falsy."""
    value = _ph.hash(code + PEPPER) if code else None
    try:
        row = OwnerLock.query.first()
        if not row:
            db_session.add(OwnerLock(passcode_hash=value))
        else:
            row.passcode_hash = value
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()
    _cache.invalidate(_KEY)
