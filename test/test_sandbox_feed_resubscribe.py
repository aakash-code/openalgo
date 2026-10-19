"""The sandbox engine retries a feed subscription the feed refused.

Measured live on 19-Oct-2026: OpenAlgo started at 08:52, before the broker
login, and the engine subscribed its nine open positions to a feed that had no
broker session yet. The feed refused (503) and nothing ever asked again, so
from 09:15 the positions had no ticks: every position book valued them with a
quote call to the broker, queued behind the option chains and chart history of
the open, and pending sandbox orders filled only through the polling fallback.
"""

from __future__ import annotations

import pytest

USER = "resubscribe_trader"


@pytest.fixture
def engine(monkeypatch):
    import database.auth_db as auth_db
    from sandbox import websocket_execution_engine as wse

    monkeypatch.setattr(auth_db, "get_api_key_for_tradingview", lambda user_id: "key")
    monkeypatch.setattr(auth_db, "get_broker_name", lambda api_key: "upstox")
    calls = []
    answers = []

    def subscribe(username, broker, symbols, mode):
        calls.append(sorted((s["symbol"], s["exchange"]) for s in symbols))
        ok = answers.pop(0) if answers else True
        return (ok, {"message": "feed not ready"}, 200 if ok else 503)

    monkeypatch.setattr(wse, "subscribe_to_symbols", subscribe)
    e = wse.WebSocketExecutionEngine()
    e._running = True
    e.calls, e.answers = calls, answers
    return e


def _hold(engine, *keys):
    for key in keys:
        engine._increment_user_symbol_refcount(USER, key)
    return [(k.split(":", 1)[1], k.split(":", 1)[0]) for k in keys]


def test_a_refused_subscription_is_sent_again_until_the_feed_takes_it(engine):
    symbols = _hold(engine, "NSE:SBIN", "NFO:NIFTY19OCT2623000CE")
    engine.answers.extend([False, False])  # before the login, and once more
    engine._subscribe_ws_symbols(USER, symbols)
    engine._retry_refused_subscriptions(force=True)
    engine._retry_refused_subscriptions(force=True)  # the feed is ready now
    engine._retry_refused_subscriptions(force=True)  # nothing left to send
    want = sorted(symbols)
    assert engine.calls == [want, want, want]


def test_the_retry_sends_only_what_is_still_held(engine):
    symbols = _hold(engine, "NSE:SBIN", "NSE:INFY")
    engine.answers.append(False)
    engine._subscribe_ws_symbols(USER, symbols)
    engine._decrement_user_symbol_refcount(USER, "NSE:INFY")  # position closed meanwhile
    engine._retry_refused_subscriptions(force=True)
    assert engine.calls[-1] == [("SBIN", "NSE")]


def test_nothing_is_retried_when_nothing_was_refused(engine):
    engine._subscribe_ws_symbols(USER, _hold(engine, "NSE:SBIN"))
    engine._retry_refused_subscriptions(force=True)
    assert len(engine.calls) == 1


def test_the_retry_waits_its_interval(engine, monkeypatch):
    from sandbox import websocket_execution_engine as wse

    now = [1000.0]
    monkeypatch.setattr(wse.time, "monotonic", lambda: now[0])
    engine.answers.append(False)
    engine._subscribe_ws_symbols(USER, _hold(engine, "NSE:SBIN"))
    engine._retry_refused_subscriptions()
    assert len(engine.calls) == 1  # the health monitor's next pass is too soon
    now[0] += wse.RESUBSCRIBE_SECONDS
    engine._retry_refused_subscriptions()
    assert len(engine.calls) == 2


def test_a_feed_that_keeps_refusing_is_reported_once(engine, caplog):
    engine.answers.extend([False, False, False])
    engine._subscribe_ws_symbols(USER, _hold(engine, "NSE:SBIN"))
    engine._retry_refused_subscriptions(force=True)
    engine._retry_refused_subscriptions(force=True)
    warnings = [r for r in caplog.records if "subscribe failed" in r.getMessage()]
    assert len(warnings) == 1
