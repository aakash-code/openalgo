"""Signal broadcast formatting and vocabulary guard.

The guard test is the one that matters commercially: it asserts no upstream data
provider's vocabulary can reach a public Telegram channel, and that quantity is
never published (subscribers size against their own capital).
"""

import re

import pytest

from services.signal_broadcast_service import format_signal

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


def test_open_message_contains_the_trade_plan(signal):
    msg = format_signal(signal)
    assert "LONG" in msg
    assert "TATAMOTORS" in msg
    assert "NIFTY AUTO" in msg
    assert "742.3" in msg  # entry
    assert "735.1" in msg  # stop
    assert "756.7" in msg  # target
    assert "10:35 IST" in msg  # rendered from the +05:30 timestamp, not UTC


def test_invalidated_message_refers_back_to_the_original_signal(signal):
    signal["event"] = "invalidated"
    msg = format_signal(signal)
    assert "CLOSED" in msg
    assert "TATAMOTORS" in msg
    assert "10:35" in msg
    # An invalidation must not read like a fresh entry.
    assert "Entry" not in msg
    assert "Target" not in msg


def test_no_upstream_vocabulary_reaches_telegram(signal):
    for event in ("open", "invalidated"):
        signal["event"] = event
        assert not BANNED.search(format_signal(signal)), f"leaked vocabulary in {event} message"


def test_quantity_is_never_published(signal):
    # Even if a quantity somehow rides along on the payload, it must not render.
    signal["qty"] = 138
    msg = format_signal(signal)
    assert "138" not in msg
    assert "qty" not in msg.lower()
    assert "quantity" not in msg.lower()


def test_broadcast_is_disabled_when_no_channel_configured(signal, monkeypatch):
    from services import signal_broadcast_service

    monkeypatch.delenv("SIGNAL_CHANNEL_ALL", raising=False)
    assert signal_broadcast_service.broadcast_signal(signal) == {"all": "disabled"}


def test_broadcast_never_raises_when_telegram_fails(signal, monkeypatch):
    from services import signal_broadcast_service

    monkeypatch.setenv("SIGNAL_CHANNEL_ALL", "-1001234567890")

    def boom(*_a, **_k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(
        signal_broadcast_service.telegram_alert_service, "send_alert_sync", boom
    )
    # A Telegram outage must not fail ingest — the row is stored and reachable
    # through the backfill endpoint regardless.
    assert signal_broadcast_service.broadcast_signal(signal) == {"all": "error"}


def test_channel_id_is_passed_through_unchanged(signal, monkeypatch):
    from services import signal_broadcast_service

    monkeypatch.setenv("SIGNAL_CHANNEL_ALL", "-1001234567890")
    seen = {}

    def capture(chat_id, message):
        seen["chat_id"] = chat_id
        seen["message"] = message
        return True

    monkeypatch.setattr(
        signal_broadcast_service.telegram_alert_service, "send_alert_sync", capture
    )
    assert signal_broadcast_service.broadcast_signal(signal) == {"all": "sent"}
    # Negative channel ids must survive intact — send_alert_sync puts this
    # straight into the sendMessage chat_id field.
    assert seen["chat_id"] == "-1001234567890"
