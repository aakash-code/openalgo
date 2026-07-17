"""Tick replay service — returns recently-recorded ticks for gap backfill.

After a feed stall the browser reconnects but has lost the ticks that arrived
during the gap. This service reads them back from the optional Redis tick tape
(``websocket_proxy.tick_store``) so the client can rebuild footprint / tape data
for the missed window.

Returns an empty tick list (not an error) when the tape is disabled or Redis is
unavailable — the caller then simply has no gap data to merge, which is safe.
"""

from __future__ import annotations

from database.auth_db import get_auth_token_broker
from utils.logging import get_logger

logger = get_logger(__name__)


def replay_ticks_service(
    api_key: str,
    symbol: str,
    exchange: str,
    since_ms: float = 0.0,
    limit: int = 5000,
) -> tuple[bool, dict, int]:
    """Validate the API key and return recorded ticks for the symbol.

    Returns (success, response_dict, http_status).
    """
    # Auth: a valid OpenAlgo apikey is required (same gate as /quotes).
    auth_token, _feed_token, _broker = get_auth_token_broker(api_key, include_feed_token=True)
    if auth_token is None:
        return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403

    try:
        # Imported lazily so a Redis-less deployment never fails to import.
        from websocket_proxy.tick_store import replay_ticks

        ticks = replay_ticks(exchange, symbol, since_ms, limit)
        return (
            True,
            {
                "status": "success",
                "symbol": symbol,
                "exchange": exchange,
                "since": since_ms,
                "count": len(ticks),
                "ticks": ticks,
            },
            200,
        )
    except Exception as e:
        logger.exception(f"Tick replay failed for {exchange}:{symbol}: {e}")
        # Degrade gracefully — no gap data, but not a hard failure.
        return (
            True,
            {
                "status": "success",
                "symbol": symbol,
                "exchange": exchange,
                "since": since_ms,
                "count": 0,
                "ticks": [],
            },
            200,
        )
