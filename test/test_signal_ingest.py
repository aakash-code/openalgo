"""Signal ingest idempotency.

The property under test is commercial, not cosmetic: the producer retries on
network failure, so if a duplicate POST re-inserted and re-broadcast, every
retry would post the same signal to a public Telegram channel again.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from database import signal_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Bind signal_db to a throwaway sqlite file for the duration of a test."""
    engine = create_engine(f"sqlite:///{tmp_path / 'signals.db'}")
    session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    signal_db.Base.metadata.create_all(engine)
    monkeypatch.setattr(signal_db, "db_session", session)
    yield session
    session.remove()


def make(event_id="TATAMOTORS:1785000000000:open", **over):
    payload = {
        "eventId": event_id,
        "event": "open",
        "symbol": "TATAMOTORS",
        "exchange": "NSE",
        "sector": "NIFTY AUTO",
        "side": "LONG",
        "entry": 742.3,
        "stop": 735.1,
        "target": 756.7,
        "stopPct": 0.97,
        "signalTimeIst": "2026-07-31T10:35:00+05:30",
    }
    payload.update(over)
    return payload


def test_first_insert_returns_the_row(db):
    row = signal_db.insert_signal_event(make())
    assert row is not None
    assert row.id == 1
    assert row.symbol == "TATAMOTORS"


def test_duplicate_event_id_returns_none_and_stores_once(db):
    assert signal_db.insert_signal_event(make()) is not None
    # The retry path: same eventId, must not create a second row.
    assert signal_db.insert_signal_event(make()) is None
    assert len(signal_db.get_events_since(0)) == 1


def test_open_and_invalidated_for_one_signal_are_distinct_events(db):
    signal_db.insert_signal_event(make())
    row = signal_db.insert_signal_event(
        make(event_id="TATAMOTORS:1785000000000:invalidated", event="invalidated")
    )
    assert row is not None
    assert len(signal_db.get_events_since(0)) == 2


def test_backfill_cursor_returns_only_newer_events_in_order(db):
    for i in range(5):
        signal_db.insert_signal_event(make(event_id=f"SYM{i}:1:open", symbol=f"SYM{i}"))

    ids = [r.id for r in signal_db.get_events_since(0)]
    assert ids == sorted(ids), "backfill must be ordered by cursor"

    after_two = signal_db.get_events_since(2)
    assert [r.id for r in after_two] == [3, 4, 5]
    assert signal_db.get_events_since(5) == []


def test_backfill_limit_is_bounded(db):
    for i in range(10):
        signal_db.insert_signal_event(make(event_id=f"SYM{i}:1:open", symbol=f"SYM{i}"))
    assert len(signal_db.get_events_since(0, limit=3)) == 3
    # A caller asking for a million rows must not get them.
    assert len(signal_db.get_events_since(0, limit=10_000)) == 10


def test_serialised_event_carries_no_quantity_or_upstream_vocabulary(db):
    row = signal_db.insert_signal_event(make())
    data = row.to_dict()
    assert "qty" not in data and "quantity" not in data
    assert set(data) == {
        "id", "eventId", "event", "symbol", "exchange", "sector", "side",
        "entry", "stop", "target", "stopPct", "signalTimeIst",
    }


def test_duplicate_does_not_rebroadcast(db, monkeypatch):
    """The end-to-end guarantee: a retried ingest posts to Telegram exactly once."""
    from services import signal_ingest_service

    monkeypatch.setattr(
        signal_ingest_service, "get_auth_token_broker", lambda *a, **k: ("tok", "feed", "upstox")
    )
    sent = []
    monkeypatch.setattr(
        signal_ingest_service, "broadcast_signal", lambda p: sent.append(p["eventId"]) or {"all": "sent"}
    )

    ok1, resp1, code1 = signal_ingest_service.ingest_signal_service("key", make())
    ok2, resp2, code2 = signal_ingest_service.ingest_signal_service("key", make())

    assert (ok1, code1) == (True, 200)
    assert (ok2, code2) == (True, 200), "a retry must not look like a failure"
    assert resp2.get("duplicate") is True
    assert sent == ["TATAMOTORS:1785000000000:open"], "broadcast must happen exactly once"
