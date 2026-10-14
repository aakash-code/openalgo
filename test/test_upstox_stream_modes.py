"""Upstox holds ONE feed mode per instrument key; OpenAlgo holds one subscription
per (symbol, exchange, mode). These tests pin how the adapter reconciles the two.

Found live on 14-Oct-2026: a strategy streamed NIFTY in LTP mode while a chart
held NIFTY in Quote mode. When the chart dropped its Quote subscription the
adapter sent Upstox an `unsub` for the NIFTY key, Upstox stopped NIFTY for both,
and the adapter, still holding the LTP subscription, answered every later
subscribe with "already subscribed". The strategy's feed looked live and sent
nothing until something else happened to subscribe NIFTY again.

The real adapter and the real client run here; only the socket is fake, and it
records every frame that would have gone to Upstox.
"""

import json
import threading

import pytest

import websocket_proxy  # noqa: F401  (must load before broker.*.streaming)
from broker.upstox.streaming import upstox_adapter
from broker.upstox.streaming.upstox_adapter import UpstoxWebSocketAdapter
from broker.upstox.streaming.upstox_client import UpstoxWebSocketClient
from utils.logging import get_logger

NIFTY_KEY = "NSE_INDEX|Nifty 50"


class FakeSocket:
    """Stands in for the websocket: records each frame the client sends."""

    def __init__(self):
        self.frames = []

    def send(self, payload, opcode=None):
        self.frames.append(json.loads(payload.decode("utf-8")))


@pytest.fixture
def adapter(monkeypatch):
    # No database: every symbol resolves to the NIFTY index key.
    monkeypatch.setattr(
        upstox_adapter.SymbolMapper,
        "get_token_from_symbol",
        staticmethod(lambda symbol, exchange: {"token": "Nifty 50", "brexchange": "NSE_INDEX"}),
    )
    client = UpstoxWebSocketClient("test-token")
    client.ws = FakeSocket()
    client._connected = True

    a = UpstoxWebSocketAdapter.__new__(UpstoxWebSocketAdapter)
    a.logger = get_logger("upstox_websocket_test")
    a.ws_client = client
    a.subscriptions = {}
    a.connected = True
    a.running = True
    a.lock = threading.Lock()
    a.subscription_queue = []
    a.batch_timer = None
    a.batch_delay = 60  # the test drains the batch itself
    a._batch_generation = 0
    a._last_ltpc = {}
    a._subs_by_instrument = {}
    a._subs_by_token = {}
    a._depth_downgrade_warned = False
    yield a
    with a.lock:
        if a.batch_timer is not None:
            a.batch_timer.cancel()


def subscribe(a, mode):
    assert a.subscribe("NIFTY", "NSE_INDEX", mode=mode)["status"] == "success"
    a._process_batch_subscriptions()


def frames(a):
    return [(f["method"], f["data"].get("mode"), f["data"]["instrumentKeys"]) for f in a.ws_client.ws.frames]


def test_dropping_one_mode_keeps_the_other_subscribers_stream(adapter):
    subscribe(adapter, 1)  # strategy: LTP
    subscribe(adapter, 2)  # chart: Quote
    adapter.ws_client.ws.frames.clear()

    adapter.unsubscribe("NIFTY", "NSE_INDEX", mode=2)  # the chart goes away

    sent = frames(adapter)
    assert ("unsub", None, [NIFTY_KEY]) not in sent, f"LTP subscriber's stream was cut: {sent}"
    # The key is moved down to what the LTP subscriber needs, not dropped.
    assert sent == [("change_mode", "ltpc", [NIFTY_KEY])], sent
    assert "NIFTY_NSE_INDEX_1" in adapter.subscriptions


def test_the_last_subscriber_leaving_unsubscribes_upstream(adapter):
    subscribe(adapter, 1)
    subscribe(adapter, 2)
    adapter.unsubscribe("NIFTY", "NSE_INDEX", mode=2)
    adapter.ws_client.ws.frames.clear()

    adapter.unsubscribe("NIFTY", "NSE_INDEX", mode=1)

    assert frames(adapter) == [("unsub", None, [NIFTY_KEY])]
    assert adapter.subscriptions == {}


def test_an_ltp_subscribe_never_downgrades_a_key_a_quote_client_holds(adapter):
    subscribe(adapter, 2)  # chart: Quote -> full
    adapter.ws_client.ws.frames.clear()

    subscribe(adapter, 1)  # strategy: LTP

    # Nothing to send: `full` already serves LTP. Sending `ltpc` would move the
    # key to ltpc, and the Quote subscriber would receive nothing.
    assert frames(adapter) == [], frames(adapter)


def test_a_quote_subscribe_on_an_ltp_key_changes_its_mode(adapter):
    subscribe(adapter, 1)
    adapter.ws_client.ws.frames.clear()

    subscribe(adapter, 2)

    # Already subscribed upstream, so it is a mode change, not a second `sub`.
    assert frames(adapter) == [("change_mode", "full", [NIFTY_KEY])], frames(adapter)
    assert adapter.ws_client._keys_by_mode.get("ltpc", set()) == set()
    assert adapter.ws_client._keys_by_mode["full"] == {NIFTY_KEY}


def test_reconnect_replays_each_key_once_in_its_richest_mode(adapter):
    subscribe(adapter, 1)
    subscribe(adapter, 2)
    # A fresh connection: the client has forgotten everything it sent.
    adapter.ws_client._keys_by_mode.clear()
    adapter.ws_client._subscriptions.clear()
    adapter.ws_client.ws.frames.clear()

    adapter._on_connect()

    assert frames(adapter) == [("sub", "full", [NIFTY_KEY])], frames(adapter)
