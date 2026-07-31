# services/signal_webhook_service.py
"""Push signals to subscriber platforms over HMAC-signed webhooks.

Delivery is best-effort by design. There is no delivery-attempt table and no
durable retry queue: the /signals/v1/events backfill endpoint IS the recovery
mechanism, so a subscriber that misses a push re-fetches by cursor. Building
both would pay twice for one guarantee.

Signing follows the usual construction: the timestamp is inside the MAC, not
merely alongside it, so a captured request cannot be replayed later with a
fresh header. Subscribers should reject a timestamp outside a few minutes.
"""

import hashlib
import hmac
import json
import threading
import time

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

RETRY_DELAYS_S = (2, 8, 30)
REQUEST_TIMEOUT_S = 10


def sign_payload(secret: str, timestamp: str, body: str) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>", hex, prefixed with the algorithm."""
    mac = hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"sha256={mac}"


def _deliver(url: str, secret: str, body: str, event_id: str) -> bool:
    client = get_httpx_client()
    for attempt, delay in enumerate(RETRY_DELAYS_S, start=1):
        timestamp = str(int(time.time()))
        try:
            resp = client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Signal-Timestamp": timestamp,
                    "X-Signal-Signature": sign_payload(secret, timestamp, body),
                },
                timeout=REQUEST_TIMEOUT_S,
            )
            if 200 <= resp.status_code < 300:
                return True
            # A 4xx other than 429 means the subscriber rejected the payload;
            # resending an identical body cannot change that.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                logger.warning(
                    f"Webhook {url} rejected {event_id} with {resp.status_code} — not retrying"
                )
                return False
            logger.warning(f"Webhook {url} returned {resp.status_code} for {event_id}")
        except Exception as e:
            logger.warning(f"Webhook {url} failed for {event_id} (attempt {attempt}): {e}")

        if attempt < len(RETRY_DELAYS_S):
            time.sleep(delay)

    logger.error(f"Webhook {url} gave up on {event_id}; subscriber must use backfill")
    return False


def dispatch_signal(payload: dict) -> int:
    """Push one signal to every subscriber with a webhook. Returns how many were queued.

    Runs off-thread: a slow or hanging subscriber must never delay the ingest
    response or the Telegram broadcast.
    """
    from database.signal_db import decrypt_hmac_secret, get_webhook_subscribers

    try:
        subscribers = get_webhook_subscribers()
    except Exception as e:
        logger.exception(f"Failed to load webhook subscribers: {e}")
        return 0

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    queued = 0
    for sub in subscribers:
        secret = decrypt_hmac_secret(sub)
        if not secret:
            logger.warning(f"Subscriber {sub.id} has no usable HMAC secret — skipping")
            continue
        threading.Thread(
            target=_deliver,
            args=(sub.webhook_url, secret, body, payload.get("eventId", "?")),
            daemon=True,
            name=f"signal-webhook-{sub.id}",
        ).start()
        queued += 1
    return queued
