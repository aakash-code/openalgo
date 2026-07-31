# services/signal_summary_service.py
"""Periodic grouped summary of open signals, posted to the Telegram channel.

Follows the scheduler pattern established by services/tf_boost_snapshot_service.py:
an isolated in-memory-jobstore BackgroundScheduler on IST, a weekday market-hours
cron, an in-job window guard for the exact 09:15-15:30 boundaries, and an opt-in
env flag so the job never starts unless deliberately enabled.

The active set is derived from the event log (database.signal_db.get_active_signals)
rather than tracked in memory, so a restart mid-session posts a correct summary
on the very next tick.
"""

import os
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone

from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone("Asia/Kolkata")

_scheduler: BackgroundScheduler | None = None


def _within_market_window(now_ist: datetime) -> bool:
    """09:15-15:30 IST on a weekday. The cron is coarser (hour granularity)."""
    if now_ist.weekday() > 4:
        return False
    minutes = now_ist.hour * 60 + now_ist.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def run_summary_tick() -> dict:
    """Build and queue one summary. Returns a small result dict for tests/logs."""
    # Imported here so the module can be loaded without touching the database.
    from database.signal_db import get_active_signals
    from services.signal_broadcast_service import broadcast_summary

    now_ist = datetime.now(IST)
    if not _within_market_window(now_ist):
        return {"status": "skipped", "reason": "outside market window"}

    try:
        rows = get_active_signals(now_ist.strftime("%Y-%m-%d"))
    except Exception as e:
        logger.exception(f"Failed to read active signals for summary: {e}")
        return {"status": "error"}

    active = [r.to_dict() for r in rows]
    delivery = broadcast_summary(active, now_ist.strftime("%H:%M"))
    logger.info(f"Signal summary: {len(active)} active, delivery={delivery}")
    return {"status": "ok", "count": len(active), "delivery": delivery}


def init_signal_summary_scheduler() -> None:
    """Start the summary job. Idempotent; opt-in via SIGNAL_SUMMARY_ENABLED."""
    global _scheduler

    if os.getenv("SIGNAL_SUMMARY_ENABLED", "false").strip().lower() != "true":
        logger.debug("Signal summary scheduler disabled (SIGNAL_SUMMARY_ENABLED)")
        return
    if _scheduler is not None:
        return

    every_minutes = max(1, int(os.getenv("SIGNAL_SUMMARY_EVERY_MINUTES", "15")))

    _scheduler = BackgroundScheduler(timezone=IST)
    _scheduler.add_job(
        run_summary_tick,
        CronTrigger(
            day_of_week="mon-fri", hour="9-15", minute=f"*/{every_minutes}", timezone=IST
        ),
        id="signal_summary_tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    _scheduler.start()
    logger.info(f"Signal summary scheduler started (every {every_minutes} min, IST market hours)")


def shutdown_signal_summary_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
