"""Grouped channel summary: active-set derivation, formatting and pacing."""

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from database import signal_db
from services.signal_broadcast_service import format_summary

BANNED = re.compile(r"tradefinder|sector.?scope|r.?factor|rfactor|param_\d|_r_factor", re.I)

TODAY = "2026-07-31"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'signals.db'}")
    session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    signal_db.Base.metadata.create_all(engine)
    monkeypatch.setattr(signal_db, "db_session", session)
    yield session
    session.remove()


def event(symbol, event="open", ts="1", sector="NIFTY AUTO", hhmm="10:35", date=TODAY):
    return {
        "eventId": f"{symbol}:{ts}:{event}",
        "event": event,
        "symbol": symbol,
        "exchange": "NSE",
        "sector": sector,
        "side": "LONG",
        "entry": 742.3,
        "stop": 735.1,
        "target": 756.7,
        "stopPct": 0.97,
        "signalTimeIst": f"{date}T{hhmm}:00+05:30",
    }


class TestActiveSet:
    def test_open_signal_is_active(self, db):
        signal_db.insert_signal_event(event("TATAMOTORS"))
        assert [r.symbol for r in signal_db.get_active_signals(TODAY)] == ["TATAMOTORS"]

    def test_invalidated_signal_drops_out(self, db):
        signal_db.insert_signal_event(event("TATAMOTORS"))
        signal_db.insert_signal_event(event("TATAMOTORS", event="invalidated"))
        assert signal_db.get_active_signals(TODAY) == []

    def test_reentry_after_invalidation_is_active_again(self, db):
        signal_db.insert_signal_event(event("TATAMOTORS"))
        signal_db.insert_signal_event(event("TATAMOTORS", event="invalidated"))
        signal_db.insert_signal_event(event("TATAMOTORS", ts="2", hhmm="11:20"))
        rows = signal_db.get_active_signals(TODAY)
        assert [r.symbol for r in rows] == ["TATAMOTORS"]
        assert rows[0].signal_time_ist.endswith("11:20:00+05:30")

    def test_yesterdays_signals_are_excluded(self, db):
        signal_db.insert_signal_event(event("INFY", date="2026-07-30"))
        signal_db.insert_signal_event(event("TATAMOTORS"))
        assert [r.symbol for r in signal_db.get_active_signals(TODAY)] == ["TATAMOTORS"]

    def test_multiple_symbols_tracked_independently(self, db):
        signal_db.insert_signal_event(event("TATAMOTORS"))
        signal_db.insert_signal_event(event("INFY", sector="NIFTY IT"))
        signal_db.insert_signal_event(event("INFY", event="invalidated", sector="NIFTY IT"))
        signal_db.insert_signal_event(event("WIPRO", sector="NIFTY IT"))
        assert sorted(r.symbol for r in signal_db.get_active_signals(TODAY)) == [
            "TATAMOTORS",
            "WIPRO",
        ]


class TestSummaryFormat:
    def active(self):
        return [
            {**event("TATAMOTORS"), "sector": "NIFTY AUTO"},
            {**event("M&M"), "sector": "NIFTY AUTO"},
            {**event("INFY"), "sector": "NIFTY IT", "side": "SHORT"},
        ]

    def test_lists_all_stocks_then_breaks_down_by_sector(self):
        msg = format_summary(self.active(), "14:32")
        assert "ACTIVE SIGNALS · 14:32 IST" in msg
        assert "3 stocks" in msg

        all_at = msg.index("ALL")
        auto_at = msg.index("NIFTY AUTO")
        it_at = msg.index("NIFTY IT")
        assert all_at < auto_at < it_at, "ALL section must come before the sector breakdown"

        # Every stock appears twice: once in ALL, once under its sector.
        assert msg.count("TATAMOTORS") == 2
        assert msg.count("INFY") == 2

    def test_sector_rows_carry_the_full_plan(self):
        msg = format_summary(self.active(), "14:32")
        assert "SL 735.1" in msg
        assert "T 756.7" in msg

    def test_empty_state_is_explicit(self):
        msg = format_summary([], "09:20")
        assert "No open signals" in msg

    def test_direction_is_visible_per_stock(self):
        msg = format_summary(self.active(), "14:32")
        assert "▲" in msg and "▼" in msg

    def test_never_publishes_quantity_or_upstream_vocabulary(self):
        rows = [{**s, "qty": 138} for s in self.active()]
        msg = format_summary(rows, "14:32")
        assert not BANNED.search(msg)
        assert "138" not in msg

    def test_long_list_is_truncated_on_a_line_boundary(self):
        many = [
            {**event(f"SYMBOL{i:03d}"), "sector": f"SECTOR {i % 12}"} for i in range(400)
        ]
        msg = format_summary(many, "14:32")
        assert len(msg) <= 4096, "must fit Telegram's message limit"
        assert msg.endswith("… list truncated")


class TestPacing:
    def test_queued_messages_coalesce_into_one_send(self, monkeypatch):
        """A burst must become one post, not one post per signal.

        Telegram allows ~20 messages/minute per channel; ten signals firing in
        the same minute would otherwise take half a minute to drain.
        """
        import queue as queue_mod

        from services import signal_broadcast_service as svc

        monkeypatch.setenv("SIGNAL_CHANNEL_ALL", "-1001234567890")
        monkeypatch.setattr(svc, "MIN_SEND_INTERVAL_S", 0.01)
        monkeypatch.setattr(svc, "_send_queue", queue_mod.Queue(maxsize=500))
        monkeypatch.setattr(svc, "_worker", None)

        sends = []
        monkeypatch.setattr(
            svc.telegram_alert_service,
            "send_alert_sync",
            lambda chat_id, message: sends.append(message) or True,
        )

        for i in range(10):
            svc.enqueue_message(f"signal {i}")
        svc._send_queue.join()

        assert len(sends) < 10, "burst must coalesce rather than post once per signal"
        combined = "\n".join(sends)
        for i in range(10):
            assert f"signal {i}" in combined, "coalescing must not drop signals"

    def test_disabled_when_no_channel_configured(self, monkeypatch):
        from services import signal_broadcast_service as svc

        monkeypatch.delenv("SIGNAL_CHANNEL_ALL", raising=False)
        assert svc.enqueue_message("anything") is False
