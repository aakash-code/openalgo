"""Stale-feed auto-recovery in the WebSocket proxy (websocket_proxy/server.py).

Covers the failure observed live on 2026-07-16: the broker WebSocket dies
silently at the daily ~03:00 IST token rollover, the adapter keeps reporting
connected=True, every subscribe "succeeds", and zero ticks flow until an app
restart. _recover_stale_adapter() must tear the zombie down, rebuild with
fresh credentials and replay live subscriptions — with exponential backoff and
a reset once ticks flow again.
"""

import json
import time
from unittest.mock import patch

import pytest

from websocket_proxy.server import WebSocketProxy


class FakeAdapter:
    def __init__(self):
        self.connected = True
        self.disconnected = False
        self.initialized_with = None
        self.subscribed = []

    def disconnect(self):
        self.disconnected = True
        self.connected = False

    def initialize(self, broker, user_id):
        self.initialized_with = (broker, user_id)
        return {"status": "success"}

    def connect(self):
        self.connected = True
        return {"status": "success"}

    def subscribe(self, symbol, exchange, mode, depth):
        self.subscribed.append((symbol, exchange, mode, depth))
        return {"status": "success"}

    def clear_auth_cache_for_user(self, user_id):
        pass


@pytest.fixture
def proxy():
    # Build the object without running __init__'s full ZMQ/network setup.
    p = WebSocketProxy.__new__(WebSocketProxy)
    p.last_tick_time = {}
    p._last_stale_warn = {}
    p._stale_tick_warn_seconds = 120
    p._last_stale_check = 0.0
    p._stale_check_interval = 30
    p._stale_recover_seconds = 300
    p._recover_state = {}
    p.broker_adapters = {}
    p.user_broker_mapping = {}
    p.user_mapping = {}
    p.subscriptions = {}
    p.subscription_index = {}
    return p


def wire_user(proxy, user_id="admin", broker="upstox", n_symbols=3):
    zombie = FakeAdapter()
    proxy.broker_adapters[user_id] = zombie
    proxy.user_broker_mapping[user_id] = broker
    client_id = 111
    proxy.user_mapping[client_id] = user_id
    subs = set()
    for i in range(n_symbols):
        subs.add(
            json.dumps(
                {
                    "symbol": f"SYM{i}",
                    "exchange": "NSE",
                    "mode": 2,
                    "depth_level": 5,
                    "broker": broker,
                }
            )
        )
        proxy.subscription_index[(f"SYM{i}", "NSE", 2)] = {client_id}
    proxy.subscriptions[client_id] = subs
    return zombie


def run_stale_check(proxy):
    proxy._last_stale_check = 0.0  # bypass the 30s check interval
    with patch("websocket_proxy.server.create_broker_adapter") as mk, patch(
        "websocket_proxy.broker_factory.cleanup_pools_for_user"
    ) as cleanup:
        fresh = FakeAdapter()
        mk.return_value = fresh
        # _clear_auth_cache_for_user touches DB caches — stub it out.
        with patch.object(WebSocketProxy, "_clear_auth_cache_for_user", lambda self, uid: None):
            proxy._log_stale_adapters()
    return fresh, cleanup


def test_recovers_silent_adapter_and_replays_subscriptions(proxy):
    zombie = wire_user(proxy)
    proxy.last_tick_time["admin"] = time.time() - 400  # silent past the 300s threshold

    fresh, cleanup = run_stale_check(proxy)

    assert zombie.disconnected, "zombie adapter must be torn down"
    assert cleanup.called, "pooled broker connection must be purged"
    assert proxy.broker_adapters["admin"] is fresh, "fresh adapter must replace the zombie"
    assert fresh.initialized_with == ("upstox", "admin")
    assert sorted(fresh.subscribed) == [
        ("SYM0", "NSE", 2, 5),
        ("SYM1", "NSE", 2, 5),
        ("SYM2", "NSE", 2, 5),
    ], "every live subscription must be replayed onto the fresh connection"
    # Stale clock restarted so a still-dead rebuild re-triggers later
    assert time.time() - proxy.last_tick_time["admin"] < 5
    # Backoff armed
    assert proxy._recover_state["admin"]["attempts"] == 1
    assert proxy._recover_state["admin"]["next_allowed"] > time.time()


def test_backoff_blocks_immediate_second_recovery(proxy):
    wire_user(proxy)
    proxy.last_tick_time["admin"] = time.time() - 400
    fresh1, _ = run_stale_check(proxy)

    # Still silent (simulate), but next_allowed is in the future → no new attempt
    proxy.last_tick_time["admin"] = time.time() - 400
    fresh2, _ = run_stale_check(proxy)
    assert proxy.broker_adapters["admin"] is fresh1, "backoff must block an immediate retry"
    assert proxy._recover_state["admin"]["attempts"] == 1

    # Once the window passes, the next attempt runs and the backoff doubles
    proxy._recover_state["admin"]["next_allowed"] = time.time() - 1
    fresh3, _ = run_stale_check(proxy)
    assert proxy.broker_adapters["admin"] is fresh3
    assert proxy._recover_state["admin"]["attempts"] == 2


def test_quiet_feed_below_threshold_is_left_alone(proxy):
    zombie = wire_user(proxy)
    proxy.last_tick_time["admin"] = time.time() - 200  # warn-worthy, below recover threshold

    run_stale_check(proxy)

    assert proxy.broker_adapters["admin"] is zombie, "below the recover threshold: warn only"
    assert not zombie.disconnected
    assert "admin" not in proxy._recover_state


def test_no_subscriptions_means_no_recovery(proxy):
    zombie = wire_user(proxy)
    proxy.subscription_index.clear()  # nobody actually subscribed
    proxy.last_tick_time["admin"] = time.time() - 4000

    run_stale_check(proxy)

    assert proxy.broker_adapters["admin"] is zombie
    assert not zombie.disconnected


def test_failed_rebuild_is_retried_even_without_registered_adapter(proxy):
    wire_user(proxy)
    proxy.last_tick_time["admin"] = time.time() - 400

    # First attempt: create_broker_adapter returns None → rebuild fails outright
    proxy._last_stale_check = 0.0
    with patch("websocket_proxy.server.create_broker_adapter", return_value=None), patch(
        "websocket_proxy.broker_factory.cleanup_pools_for_user"
    ):
        with patch.object(WebSocketProxy, "_clear_auth_cache_for_user", lambda self, uid: None):
            proxy._log_stale_adapters()
    assert "admin" not in proxy.broker_adapters, "failed rebuild leaves no adapter registered"
    assert proxy._recover_state["admin"]["attempts"] == 1

    # Backoff passes → the no-adapter retry path must pick it up
    proxy._recover_state["admin"]["next_allowed"] = time.time() - 1
    fresh, _ = run_stale_check(proxy)
    assert proxy.broker_adapters["admin"] is fresh, "retry loop must rebuild despite missing adapter"
    assert proxy._recover_state["admin"]["attempts"] == 2


def test_disabled_recovery_only_warns(proxy):
    zombie = wire_user(proxy)
    proxy._stale_recover_seconds = 0  # detection-only mode
    proxy.last_tick_time["admin"] = time.time() - 4000

    run_stale_check(proxy)

    assert proxy.broker_adapters["admin"] is zombie
    assert not zombie.disconnected
    assert "admin" not in proxy._recover_state
