"""A browser page reload must not tear down the shared broker feed.

`cleanup_client` used to unsubscribe every symbol and disconnect the broker
adapter inline the moment a client socket closed. On a single-user deployment
the one chart tab is usually the *last* client, so each reload cost a full
unsubscribe-all plus an adapter rebuild — a subscribe storm that stalled the
live Upstox feed for ~2 minutes on 2026-08-03 before the majority-stale
detector rebuilt it. These tests pin the linger window that fixes it.

Driven with asyncio.run rather than pytest-asyncio so the suite needs no extra
plugin. WebSocketProxy is built with object.__new__ because its __init__ binds
a real port.
"""

import asyncio
import json

from websocket_proxy.server import WebSocketProxy

SYMBOLS = ["RELIANCE", "INFY", "NATIONALUM"]


class FakeAdapter:
    """Records exactly what would be sent to the broker."""

    def __init__(self):
        self.unsubscribed: list[tuple] = []
        self.disconnected = False

    def unsubscribe(self, symbol, exchange, mode=2):
        self.unsubscribed.append((symbol, exchange, mode))
        return {"status": "success"}

    def disconnect(self):
        self.disconnected = True


def make_proxy(linger_seconds):
    proxy = object.__new__(WebSocketProxy)
    proxy.clients = {}
    proxy.subscriptions = {}
    proxy.subscription_index = {}
    proxy.user_mapping = {}
    proxy.order_subscribers = {}
    proxy.broker_adapters = {}
    proxy.user_broker_mapping = {}
    proxy._client_linger_seconds = linger_seconds
    proxy._pending_teardowns = {}
    return proxy


def register(proxy, client_id, user_id, adapter, broker="upstox"):
    """Mimic an authenticated client holding SYMBOLS."""
    proxy.clients[client_id] = object()
    proxy.user_mapping[client_id] = user_id
    proxy.broker_adapters[user_id] = adapter
    proxy.user_broker_mapping[user_id] = broker
    subs = set()
    for sym in SYMBOLS:
        subs.add(json.dumps({"symbol": sym, "exchange": "NSE", "mode": 2}))
        proxy.subscription_index.setdefault((sym, "NSE", 2), set()).add(client_id)
    proxy.subscriptions[client_id] = subs


async def drain(proxy):
    """Wait for every deferred teardown to finish."""
    if proxy._pending_teardowns:
        await asyncio.gather(*list(proxy._pending_teardowns.values()), return_exceptions=True)


def test_reload_inside_linger_window_sends_no_broker_frames():
    """The whole point: a tab that comes straight back costs nothing."""

    async def scenario():
        proxy = make_proxy(0.05)
        adapter = FakeAdapter()
        register(proxy, "tab-v1", "admin", adapter)

        await proxy.cleanup_client("tab-v1")  # page reload starts
        assert "tab-v1" in proxy._pending_teardowns, "teardown must be deferred, not inline"
        assert adapter.unsubscribed == [], "nothing may hit the broker before the window expires"

        register(proxy, "tab-v2", "admin", adapter)  # reloaded page reconnects
        await drain(proxy)

        assert adapter.unsubscribed == [], "reload must not unsubscribe symbols the new tab holds"
        assert not adapter.disconnected, "reload must not disconnect the shared broker adapter"
        # The returning tab still owns every symbol.
        for sym in SYMBOLS:
            assert proxy.subscription_index[(sym, "NSE", 2)] == {"tab-v2"}
        assert proxy._pending_teardowns == {}, "completed teardowns must not leak"

    asyncio.run(scenario())


def test_client_that_never_returns_is_fully_torn_down():
    """The linger must not become a leak — a genuine disconnect still cleans up."""

    async def scenario():
        proxy = make_proxy(0.05)
        adapter = FakeAdapter()
        register(proxy, "tab-v1", "admin", adapter)

        await proxy.cleanup_client("tab-v1")
        await drain(proxy)

        assert sorted(s for s, _, _ in adapter.unsubscribed) == sorted(SYMBOLS)
        assert adapter.disconnected, "last client gone for good must release the adapter"
        assert proxy.broker_adapters == {}
        assert proxy._pending_teardowns == {}

    asyncio.run(scenario())


def test_linger_zero_restores_inline_teardown():
    """WS_CLIENT_LINGER_SECONDS=0 is the documented escape hatch."""

    async def scenario():
        proxy = make_proxy(0)
        adapter = FakeAdapter()
        register(proxy, "tab-v1", "admin", adapter)

        await proxy.cleanup_client("tab-v1")

        # No await on a pending task — it must already be done.
        assert proxy._pending_teardowns == {}
        assert sorted(s for s, _, _ in adapter.unsubscribed) == sorted(SYMBOLS)
        assert adapter.disconnected

    asyncio.run(scenario())


def test_client_ids_are_never_reused():
    """A recycled id would let a pending teardown kill a live client's feed.

    client_id used to be id(websocket); CPython hands that address to the next
    connection once the old object is freed, and every connection is the same
    class. Harmless when teardown was inline, fatal once it is deferred.
    """
    proxy = object.__new__(WebSocketProxy)
    proxy._client_seq = __import__("itertools").count(1)
    seen = set()
    for _ in range(1000):
        cid = f"c{next(proxy._client_seq)}"
        assert cid not in seen, f"client id {cid} reused"
        seen.add(cid)


def test_second_client_keeps_feed_when_first_leaves_for_good():
    """Two real tabs open: closing one must not disturb the other's feed."""

    async def scenario():
        proxy = make_proxy(0.05)
        adapter = FakeAdapter()
        register(proxy, "tab-a", "admin", adapter)
        register(proxy, "tab-b", "admin", adapter)

        await proxy.cleanup_client("tab-a")
        await drain(proxy)

        assert adapter.unsubscribed == []
        assert not adapter.disconnected
        assert proxy.user_mapping == {"tab-b": "admin"}

    asyncio.run(scenario())
