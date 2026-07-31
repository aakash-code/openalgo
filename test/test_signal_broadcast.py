"""Instant signal alerts: formatting and the vocabulary guard.

The guard test is the one that matters commercially: it asserts no upstream data
provider's vocabulary can reach a public Telegram channel, and that quantity is
never published (subscribers size against their own capital).
"""

import queue as queue_mod
import re

import pytest

from services.signal_broadcast_service import format_alert

# Anything identifying the upstream scanner, in any casing or separator.
BANNED = re.compile(r"tradefinder|sector.?scope|r.?factor|rfactor|param_\d|_r_factor", re.I)


@pytest.fixture
def signal():
    return {
        "eventId": "TATAMOTORS:1785000000000:open",
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


@pytest.fixture
def broadcast(monkeypatch):
    """Isolated queue + worker, so tests never touch module-global state."""
    from services import signal_broadcast_service as svc

    monkeypatch.setenv("SIGNAL_CHANNEL_ALL", "-1001234567890")
    monkeypatch.setattr(svc, "MIN_SEND_INTERVAL_S", 0.01)
    monkeypatch.setattr(svc, "_send_queue", queue_mod.Queue(maxsize=500))
    monkeypatch.setattr(svc, "_worker", None)
    return svc


def test_open_alert_contains_the_trade_plan(signal):
    msg = format_alert(signal)
    assert "LONG" in msg
    assert "TATAMOTORS" in msg
    assert "NIFTY AUTO" in msg
    assert "742.3" in msg  # entry
    assert "735.1" in msg  # stop
    assert "756.7" in msg  # target
    assert "10:35 IST" in msg  # rendered from the +05:30 timestamp, not UTC


def test_invalidated_alert_refers_back_to_the_original_signal(signal):
    signal["event"] = "invalidated"
    msg = format_alert(signal)
    assert "CLOSED" in msg
    assert "TATAMOTORS" in msg
    assert "10:35" in msg
    # An invalidation must not read like a fresh entry.
    assert "Entry" not in msg
    assert "Target" not in msg


def test_no_upstream_vocabulary_reaches_telegram(signal):
    for event in ("open", "invalidated"):
        signal["event"] = event
        assert not BANNED.search(format_alert(signal)), f"leaked vocabulary in {event} message"


def test_quantity_is_never_published(signal):
    # Even if a quantity somehow rides along on the payload, it must not render.
    signal["qty"] = 138
    msg = format_alert(signal)
    assert "138" not in msg
    assert "qty" not in msg.lower()
    assert "quantity" not in msg.lower()


def test_broadcast_is_disabled_when_no_channel_configured(signal, monkeypatch):
    from services import signal_broadcast_service as svc

    monkeypatch.delenv("SIGNAL_CHANNEL_ALL", raising=False)
    assert svc.broadcast_signal(signal) == {"all": "disabled"}


def test_channel_id_is_passed_through_unchanged(signal, broadcast):
    seen = {}

    def capture(chat_id, message):
        seen["chat_id"] = chat_id
        seen["message"] = message
        return True

    broadcast.telegram_alert_service.send_alert_sync = capture

    assert broadcast.broadcast_signal(signal) == {"all": "queued"}
    broadcast._send_queue.join()

    # Negative channel ids must survive intact — send_alert_sync puts this
    # straight into the sendMessage chat_id field.
    assert seen["chat_id"] == "-1001234567890"
    assert "TATAMOTORS" in seen["message"]


def test_telegram_failure_does_not_kill_the_worker(signal, broadcast):
    """A Telegram outage must not stop later signals from being posted."""
    calls = []

    def flaky(chat_id, message):
        calls.append(message)
        if len(calls) == 1:
            raise RuntimeError("telegram down")
        return True

    broadcast.telegram_alert_service.send_alert_sync = flaky

    broadcast.broadcast_signal(signal)
    broadcast._send_queue.join()
    broadcast.broadcast_signal({**signal, "symbol": "INFY"})
    broadcast._send_queue.join()

    assert len(calls) == 2, "the worker must survive a failed send"
    assert "INFY" in calls[1]


def test_queue_drops_rather_than_growing_without_bound(signal, broadcast, monkeypatch):
    """Under a Telegram stall an unbounded queue would grow until the process died.

    The signal is already stored and reachable through the backfill endpoint, so
    dropping the channel copy is the right trade.
    """
    monkeypatch.setattr(broadcast, "_send_queue", queue_mod.Queue(maxsize=2))
    # No worker started, so nothing drains.
    monkeypatch.setattr(broadcast, "_ensure_worker", lambda: None)

    assert broadcast.enqueue_message("one") is True
    assert broadcast.enqueue_message("two") is True
    assert broadcast.enqueue_message("three") is False
