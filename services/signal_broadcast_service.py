# services/signal_broadcast_service.py
"""Format and post trading signals to Telegram channels.

Reuses telegram_alert_service.send_alert_sync verbatim: its `telegram_id` goes
straight into the sendMessage payload as chat_id, so a negative channel id works
unchanged, and it already handles the Markdown->plain-text retry.

Deliberately NOT used:
  - send_broadcast_alert — iterates telegram_users; we post to channels
  - alert_executor       — 5 unordered workers with no pacing, so a burst of
                           signals could arrive out of chronological order

Stage 2 posts to the ALL channel only and sends inline. Per-sector routing and
the pacing queue arrive in stage 3, when message volume doubles.
"""

import os

from services.telegram_alert_service import telegram_alert_service
from utils.logging import get_logger

logger = get_logger(__name__)


def _channel_all() -> str | None:
    """Chat id of the main channel, e.g. -1001234567890. Unset disables posting."""
    value = (os.getenv("SIGNAL_CHANNEL_ALL") or "").strip()
    return value or None


def format_signal(payload: dict) -> str:
    """Render a signal for Telegram.

    Carries no upstream-provider vocabulary and no quantity — subscribers size
    positions against their own capital. Sector names are NSE index names.
    A guard test asserts the rendered text stays clean.
    """
    time_hhmm = payload["signalTimeIst"][11:16]  # "…T10:35:00+05:30" -> "10:35"

    if payload["event"] == "invalidated":
        return (
            f"CLOSED · {payload['symbol']}\n"
            f"{payload['side']} idea from {time_hhmm} is no longer valid."
        )

    return (
        f"{payload['side']} · {payload['symbol']}  ({payload['sector']})\n"
        f"Entry  {payload['entry']}\n"
        f"Stop   {payload['stop']}  ({payload['stopPct']}%)\n"
        f"Target {payload['target']}\n"
        f"{time_hhmm} IST"
    )


def broadcast_signal(payload: dict) -> dict:
    """Post one signal. Returns a per-channel delivery summary.

    Never raises: a Telegram outage must not fail the ingest request or lose the
    stored row, which subscribers can still reach through the backfill endpoint.
    """
    chat_id = _channel_all()
    if not chat_id:
        logger.debug("SIGNAL_CHANNEL_ALL not set — signal stored but not broadcast")
        return {"all": "disabled"}

    message = format_signal(payload)
    try:
        ok = telegram_alert_service.send_alert_sync(chat_id, message)
        return {"all": "sent" if ok else "failed"}
    except Exception as e:
        logger.exception(f"Telegram broadcast failed for {payload.get('eventId')}: {e}")
        return {"all": "error"}
