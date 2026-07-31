# websocket_proxy/ws_liveness.py
"""Shared broker-WebSocket liveness settings.

Every broker streaming adapter had independently hardcoded the same numbers:
``ping_interval=30, ping_timeout=10`` on ``run_forever`` plus a
``HEALTH_CHECK_INTERVAL = 30`` / ``DATA_TIMEOUT = 90`` stall watchdog. That
combination means a socket that is open but dead goes unnoticed for up to 40s,
and a socket that is alive but delivering nothing for up to 90s.

For comparison, Zerodha's own reference ticker (pykiteconnect ``ticker.py``)
uses ``PING_INTERVAL = 2.5`` and treats the connection as dead once
``last_pong_diff > 2 * PING_INTERVAL`` — i.e. ~7.5s. It can afford that because
the Kite feed sends a 1-byte heartbeat every second, so "no bytes for 5s" is a
valid liveness test independent of market activity. Our brokers send no such
heartbeat, which is why each adapter feeds its liveness clock from the protocol
pong (see e.g. ``upstox_client._on_ws_pong``) — but at a 30s ping cadence that
clock has 30s of granularity, forcing the large DATA_TIMEOUT.

Tightening the ping cadence is what makes a smaller stall threshold safe: at
PING_INTERVAL=5 a DATA_TIMEOUT of 30s means six consecutive missed pongs, which
is a genuinely dead socket rather than a quiet market.

Every value is env-overridable so a broker whose server dislikes frequent pings
can be tuned back without a code change. Adapters whose server does not answer
protocol pings at all (e.g. Arrow, see its own comments) must NOT use
PING_INTERVAL/PING_TIMEOUT — only the DATA_TIMEOUT watchdog applies there.
"""
import os

# run_forever(ping_interval=..., ping_timeout=...) — dead-socket detection in
# roughly PING_INTERVAL + PING_TIMEOUT seconds.
#
# PING_TIMEOUT must be STRICTLY LESS than PING_INTERVAL: websocket-client's
# run_forever() rejects ping_interval <= ping_timeout with "Ensure
# ping_interval > ping_timeout", which kills the feed on every connect attempt.
# Enforced by _sanity() below so it can never ship again.
PING_INTERVAL = int(os.getenv("BROKER_WS_PING_INTERVAL", "5"))
PING_TIMEOUT = int(os.getenv("BROKER_WS_PING_TIMEOUT", "4"))

# Stall watchdog: how often to check, and how long without any inbound frame
# (data OR pong) before forcing a reconnect.
HEALTH_CHECK_INTERVAL = int(os.getenv("BROKER_WS_HEALTH_CHECK_INTERVAL", "5"))
DATA_TIMEOUT = int(os.getenv("BROKER_WS_DATA_TIMEOUT", "30"))


def _sanity() -> None:
    """Reject settings that would kill the feed rather than protect it."""
    # websocket-client's run_forever() raises on ping_interval <= ping_timeout,
    # and its own reconnect loop retries forever — so this misconfiguration
    # takes the tick feed down completely. Fail loudly at import instead.
    assert PING_INTERVAL > PING_TIMEOUT, (
        f"BROKER_WS_PING_INTERVAL ({PING_INTERVAL}s) must be strictly greater "
        f"than BROKER_WS_PING_TIMEOUT ({PING_TIMEOUT}s) — websocket-client "
        f"refuses to connect otherwise, killing the feed"
    )
    assert DATA_TIMEOUT >= 3 * PING_INTERVAL, (
        f"BROKER_WS_DATA_TIMEOUT ({DATA_TIMEOUT}s) must be at least 3x "
        f"BROKER_WS_PING_INTERVAL ({PING_INTERVAL}s), else a healthy but quiet "
        f"feed will reconnect in a loop"
    )
    assert HEALTH_CHECK_INTERVAL < DATA_TIMEOUT, (
        f"BROKER_WS_HEALTH_CHECK_INTERVAL ({HEALTH_CHECK_INTERVAL}s) must be "
        f"below BROKER_WS_DATA_TIMEOUT ({DATA_TIMEOUT}s) to detect a stall"
    )


_sanity()
