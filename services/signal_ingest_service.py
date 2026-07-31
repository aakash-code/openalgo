# services/signal_ingest_service.py
"""Accept a signal edge from the headless producer, store it, then fan out.

Storage-before-broadcast is the ordering that matters: the UNIQUE event_id makes
ingest idempotent, so a retried POST re-inserts nothing and therefore re-posts
nothing to Telegram. Broadcasting first would double-post on every retry.
"""

from __future__ import annotations

from database.auth_db import get_auth_token_broker
from database.signal_db import insert_signal_event
from services.signal_broadcast_service import broadcast_signal
from utils.logging import get_logger

logger = get_logger(__name__)


def ingest_signal_service(api_key: str, payload: dict) -> tuple[bool, dict, int]:
    """Validate, persist and broadcast one signal event.

    Returns the repo-standard (success, response_data, status_code).
    """
    auth_token, _feed_token, _broker = get_auth_token_broker(api_key, include_feed_token=True)
    if auth_token is None:
        return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403

    try:
        row = insert_signal_event(payload)
    except Exception as e:
        logger.exception(f"Failed to store signal {payload.get('eventId')}: {e}")
        return False, {"status": "error", "message": "Failed to store signal"}, 500

    if row is None:
        # Already ingested. Report success so the producer stops retrying, but
        # do NOT broadcast again.
        logger.info(f"Duplicate signal ignored: {payload.get('eventId')}")
        return True, {"status": "success", "duplicate": True}, 200

    delivery = broadcast_signal(payload)
    return True, {"status": "success", "id": row.id, "delivery": delivery}, 200
