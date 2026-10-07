"""Upstox GET retries once on a connection Upstox already closed.

A pooled keep-alive connection that Upstox has dropped surfaces as a read or
connect error on the next request. GET is read-only, so one retry on a fresh
connection is safe; a second failure is raised rather than retried forever.
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_KEY_PEPPER", "a" * 64)

import broker.upstox.api.data as data  # noqa: E402


class _Client:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def get(self, url, headers=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ReadError("connection reset")
        return httpx.Response(200, json={"status": "success", "data": {}})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(data, "apply_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(data.time, "sleep", lambda s: None)

    def make(failures):
        c = _Client(failures)
        monkeypatch.setattr(data, "get_httpx_client", lambda: c)
        return c

    return make


def test_a_reset_connection_is_retried_once(client):
    c = client(failures=1)
    body = data.get_api_response("/market-quote/ltp", "token")
    assert body["status"] == "success"
    assert c.calls == 2


def test_a_second_reset_is_raised_not_retried_forever(client):
    c = client(failures=2)
    with pytest.raises(httpx.ReadError):
        data.get_api_response("/market-quote/ltp", "token")
    assert c.calls == 2
