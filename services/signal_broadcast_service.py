# services/signal_broadcast_service.py
"""Format and post trading signals to a single Telegram channel.

Two message kinds, both to the same channel:
  - an instant alert when a signal opens or is invalidated, so subscribers get
    entry latency measured in seconds
  - a periodic grouped summary (all signal stocks, then broken down by sector),
    posted by services/signal_summary_service.py

Reuses telegram_alert_service.send_alert_sync verbatim: its `telegram_id` goes
straight into the sendMessage payload as chat_id, so a negative channel id works
unchanged, and it already handles the Markdown->plain-text retry.

Deliberately NOT used:
  - send_broadcast_alert — iterates telegram_users; we post to one channel
  - alert_executor       — 5 unordered workers, so a burst of alerts could
                           arrive out of chronological order

Pacing matters here because Telegram allows only ~20 messages/minute to a
channel and bot_config.rate_limit_per_minute, though stored, is read by no code
in this repo. A single worker thread enforces the interval and — the useful part
— coalesces everything that piled up during the wait into ONE message. A burst
of ten signals becomes one post a few seconds later rather than ten posts spread
over half a minute.
"""

import os
import queue
import threading
import time

from services.telegram_alert_service import telegram_alert_service
from utils.logging import get_logger

logger = get_logger(__name__)

# ~20 messages/minute per channel is Telegram's limit; 3.1s leaves headroom.
MIN_SEND_INTERVAL_S = float(os.getenv("SIGNAL_MIN_SEND_INTERVAL", "3.1"))
# Telegram hard-limits a message to 4096 characters.
MAX_MESSAGE_CHARS = 3900

_send_queue: "queue.Queue[str]" = queue.Queue(maxsize=500)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _channel_all() -> str | None:
    """Chat id of the channel, e.g. -1001234567890. Unset disables posting."""
    value = (os.getenv("SIGNAL_CHANNEL_ALL") or "").strip()
    return value or None


def _arrow(side: str) -> str:
    return "▲" if side == "LONG" else "▼"


def format_alert(payload: dict) -> str:
    """One signal, rendered for immediate posting.

    Carries the full trade plan but never a quantity — subscribers size against
    their own capital — and no upstream-provider vocabulary. Sector names are
    NSE index names. A guard test asserts the rendered text stays clean.
    """
    time_hhmm = payload["signalTimeIst"][11:16]  # "…T10:35:00+05:30" -> "10:35"

    if payload["event"] == "invalidated":
        return (
            f"CLOSED · {payload['symbol']}\n"
            f"{payload['side']} idea from {time_hhmm} is no longer valid."
        )

    return (
        f"{_arrow(payload['side'])} {payload['side']} · {payload['symbol']}  ({payload['sector']})\n"
        f"Entry  {payload['entry']}\n"
        f"Stop   {payload['stop']}  ({payload['stopPct']}%)\n"
        f"Target {payload['target']}\n"
        f"{time_hhmm} IST"
    )


def format_summary(active: list[dict], now_hhmm: str) -> str:
    """The grouped board: every open signal, then the same stocks by sector."""
    if not active:
        return f"ACTIVE SIGNALS · {now_hhmm} IST\nNo open signals right now."

    lines = [f"ACTIVE SIGNALS · {now_hhmm} IST", f"{len(active)} stocks", "", "ALL"]
    for s in sorted(active, key=lambda x: x["symbol"]):
        lines.append(f"{_arrow(s['side'])} {s['symbol']}  {s['entry']}")

    by_sector: dict[str, list[dict]] = {}
    for s in active:
        by_sector.setdefault(s["sector"], []).append(s)

    for sector in sorted(by_sector):
        lines.append("")
        lines.append(sector.upper())
        for s in sorted(by_sector[sector], key=lambda x: x["symbol"]):
            lines.append(
                f"{_arrow(s['side'])} {s['symbol']}  {s['entry']} "
                f"| SL {s['stop']} | T {s['target']}  {s['signalTimeIst'][11:16]}"
            )

    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_CHARS:
        # Truncate on a line boundary rather than mid-row, so the message never
        # ends on half a price.
        clipped = message[:MAX_MESSAGE_CHARS].rsplit("\n", 1)[0]
        message = f"{clipped}\n… list truncated"
    return message


def _drain_worker() -> None:
    """Send queued messages, coalescing whatever accumulated between sends."""
    while True:
        try:
            batch = [_send_queue.get()]
            # Everything already waiting joins this send rather than becoming
            # its own message. Non-blocking, so we never wait for more.
            while len(batch) < 20:
                try:
                    batch.append(_send_queue.get_nowait())
                except queue.Empty:
                    break

            chat_id = _channel_all()
            if chat_id:
                message = "\n\n".join(batch)
                if len(message) > MAX_MESSAGE_CHARS:
                    message = f"{message[:MAX_MESSAGE_CHARS].rsplit(chr(10), 1)[0]}\n… truncated"
                try:
                    if not telegram_alert_service.send_alert_sync(chat_id, message):
                        logger.warning(f"Telegram rejected a signal message ({len(batch)} items)")
                except Exception as e:
                    logger.exception(f"Telegram send failed for {len(batch)} items: {e}")

            for _ in batch:
                _send_queue.task_done()
        except Exception as e:  # a worker death would silently stop all posting
            logger.exception(f"Signal send worker error: {e}")

        time.sleep(MIN_SEND_INTERVAL_S)


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_drain_worker, daemon=True, name="signal-broadcast"
            )
            _worker.start()


def enqueue_message(message: str) -> bool:
    """Queue a pre-rendered message. False when dropped or posting is disabled."""
    if not _channel_all():
        logger.debug("SIGNAL_CHANNEL_ALL not set — message stored but not broadcast")
        return False
    _ensure_worker()
    try:
        _send_queue.put_nowait(message)
        return True
    except queue.Full:
        # Dropping is correct here: the signal is already stored and reachable
        # through the backfill endpoint, and an unbounded queue under a Telegram
        # outage would grow until the process died.
        logger.warning("Signal send queue full — dropping message")
        return False


def broadcast_signal(payload: dict) -> dict:
    """Post the instant alert for one signal edge. Never raises."""
    try:
        queued = enqueue_message(format_alert(payload))
        return {"all": "queued" if queued else "disabled"}
    except Exception as e:
        logger.exception(f"Failed to queue signal {payload.get('eventId')}: {e}")
        return {"all": "error"}


def broadcast_summary(active: list[dict], now_hhmm: str) -> dict:
    """Post the periodic grouped summary. Never raises."""
    try:
        queued = enqueue_message(format_summary(active, now_hhmm))
        return {"all": "queued" if queued else "disabled"}
    except Exception as e:
        logger.exception(f"Failed to queue signal summary: {e}")
        return {"all": "error"}
