"""Retry policy for the shared httpx client.

Guards the money-critical half: a GET may be replayed after a dropped pooled
connection, but a POST (place order) must NEVER be, or a transport blip becomes
a duplicate trade.
"""
import httpx
import pytest

from utils.httpx_client import _request_with_retry


class FakeClient:
    """Fails the first N calls with `error`, then returns a 200."""

    def __init__(self, error, fail_times=1):
        self.error = error
        self.fail_times = fail_times
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(method)
        if len(self.calls) <= self.fail_times:
            raise self.error
        return httpx.Response(200, request=httpx.Request(method, url))


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("[Errno 35] Resource temporarily unavailable"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ConnectError("connection refused"),
    ],
)
def test_get_is_retried_once(error):
    client = FakeClient(error)
    resp = _request_with_retry(client, "GET", "https://api.example.com/history")
    assert resp.status_code == 200
    assert client.calls == ["GET", "GET"]


def test_post_is_never_retried():
    client = FakeClient(httpx.ReadError("boom"))
    with pytest.raises(httpx.ReadError):
        _request_with_retry(client, "POST", "https://api.example.com/placeorder")
    assert client.calls == ["POST"], "an order must never be replayed"


def test_retry_gives_up_after_one_attempt():
    client = FakeClient(httpx.ReadError("boom"), fail_times=99)
    with pytest.raises(httpx.ReadError):
        _request_with_retry(client, "GET", "https://api.example.com/history")
    assert client.calls == ["GET", "GET"], "must not loop"


def test_non_transport_errors_are_not_retried():
    client = FakeClient(httpx.TimeoutException("slow"))
    with pytest.raises(httpx.TimeoutException):
        _request_with_retry(client, "GET", "https://api.example.com/history")
    assert client.calls == ["GET"]
