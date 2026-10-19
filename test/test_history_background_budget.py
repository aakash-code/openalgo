"""Background history calls stay inside what the broker allows over minutes.

Measured live on 19-Oct-2026 with Upstox, which allows 2,000 standard requests
per 30 minutes: TradeFinder's enrichment and the Writer Flow gap fill, all
background callers, took every free turn of the ~3-per-second history pacer,
about 3,600 history calls in 30 minutes. Giving way to charts decided who went
first, never how many went, so the allowance ran out and every Upstox call
behind it waited for minutes: quotes, the sandbox position book, chart history
and the Writer Flow minute beat.
"""

from __future__ import annotations

import types

import pytest

import services.history_service as hs


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    monkeypatch.setattr(hs.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(hs.time, "sleep", sleep)
    monkeypatch.setattr(hs, "_background_starts", hs.deque())
    monkeypatch.setattr(hs, "_next_history_slot", 0.0)
    return types.SimpleNamespace(now=now, slept=slept)


def test_background_calls_past_the_brokers_minute_budget_wait_for_it(clock):
    for _ in range(3):
        hs._wait_for_background_budget(3)
    assert clock.now[0] == 1000.0  # the budget's own calls go at once
    hs._wait_for_background_budget(3)
    assert clock.now[0] >= 1060.0  # the fourth waits for the first to age out


def test_a_trader_request_waits_a_bounded_time(clock):
    for _ in range(2):
        hs._wait_for_background_budget(2)
    hs._wait_for_background_budget(2, max_wait=10.0)
    assert clock.now[0] == pytest.approx(1010.0, abs=1.0)


def test_a_broker_that_declares_no_budget_is_not_capped(clock, monkeypatch):
    monkeypatch.setattr(hs, "import_broker_module", lambda name: types.SimpleNamespace())
    assert hs._background_budget("zerodha") is None


def test_upstox_declares_a_budget_inside_its_30_minute_allowance():
    import broker.upstox.api.data as upstox

    per_minute = upstox.BACKGROUND_HISTORY_PER_MINUTE
    # A history call is up to two Upstox requests; background stays well under
    # half of the 2,000 per 30 minutes, leaving the rest for charts and quotes.
    assert 0 < per_minute * 2 * 30 <= 1000


def test_get_history_in_the_background_honours_the_budget(clock, monkeypatch):
    waited = []
    monkeypatch.setattr(hs, "_background_budget", lambda broker: 15)
    monkeypatch.setattr(
        hs, "_wait_for_background_budget", lambda n, max_wait=None: waited.append((n, max_wait))
    )
    monkeypatch.setattr(hs, "get_history_with_auth", lambda *a: (True, {"data": []}, 200))
    hs.get_history(
        "SBIN",
        "NSE",
        "5m",
        "2026-10-19",
        "2026-10-19",
        auth_token="t",
        broker="upstox",
        background=True,
    )
    hs.get_history("SBIN", "NSE", "5m", "2026-10-19", "2026-10-19", auth_token="t", broker="upstox")
    assert waited == [(15, None)]  # scheduler work outside a request waits its turn


def test_a_background_call_with_an_api_key_finds_its_broker(clock, monkeypatch):
    # The Writer Flow fill passes an API key, not a broker.
    seen = []
    monkeypatch.setattr(
        hs,
        "get_auth_token_broker",
        lambda key, include_feed_token=False: (
            ("t", "f", "upstox") if include_feed_token else ("t", "upstox")
        ),
    )
    monkeypatch.setattr(hs, "_background_budget", lambda broker: seen.append(broker) or None)
    monkeypatch.setattr(hs, "get_history_with_auth", lambda *a: (True, {"data": []}, 200))
    hs.get_history("SBIN", "NSE", "1m", "2026-10-19", "2026-10-19", api_key="k", background=True)
    assert seen == ["upstox"]
