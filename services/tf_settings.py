"""TradeFinder's background work, switched on or off from the TradeFinder panel.

Background work is the 30-second snapshot of TradeFinder's lists and the
history it fetches to enrich them: CPR, first candle, directional score and
momentum setup for every boost symbol. On 19-Oct-2026 that enrichment alone
asked Upstox for thousands of history calls per 30 minutes against an
allowance of 2,000 requests for everything, and charts, quotes, the sandbox
position book and the Writer Flow recorder waited minutes behind it.

Off, the snapshot skips its ticks, the watchdog leaves the scheduler alone,
the daily checks stand down, and the panel's own requests serve whatever was
already computed instead of fetching more. The TradeFinder lists themselves
still load. On, the scheduler starts if it is not running. No restart either
way.

The switch is stored in TradeFinder's own database. An install that has never
used it follows TF_BOOST_SNAPSHOT_ENABLED in .env.
"""

from __future__ import annotations

import os

from utils import real_threading
from utils.logging import get_logger

logger = get_logger(__name__)

SETTING_KEY = "background_enabled"

# The switch as last read or written. A real lock: the snapshot scheduler's
# threads, the watchdog (a real OS thread) and requests all read it.
_lock = real_threading.Lock()
_enabled: bool | None = None


def _env_default() -> bool:
    return os.getenv("TF_BOOST_SNAPSHOT_ENABLED", "false").lower() == "true"


def background_enabled() -> bool:
    """Whether TradeFinder's background work may run. Read once, then from memory."""
    global _enabled
    with _lock:
        if _enabled is not None:
            return _enabled
    try:
        from database.tf_boost_db import read_tf_setting

        stored = read_tf_setting(SETTING_KEY)
    except Exception:
        logger.exception("TradeFinder: could not read the background switch, using .env")
        stored = None
    value = _env_default() if stored is None else stored == "true"
    with _lock:
        if _enabled is None:
            _enabled = value
        return _enabled


def set_background_enabled(on: bool) -> bool:
    """Store the switch and apply it now. Returns the value in force."""
    global _enabled
    from database.tf_boost_db import write_tf_setting

    write_tf_setting(SETTING_KEY, "true" if on else "false")
    with _lock:
        _enabled = on
    logger.info(f"TradeFinder background work switched {'on' if on else 'off'}")
    if on:
        start_background()
    return on


def start_background() -> None:
    """Start the snapshot scheduler, its daily jobs and its watchdog. Each is a
    no-op when already running, so calling it again is safe."""
    from services.tf_boost_snapshot_service import (
        init_tf_boost_daily_jobs,
        init_tf_boost_snapshot,
        init_tf_boost_watchdog,
    )

    init_tf_boost_snapshot()
    # The morning check and the post-close behaviour study ride on the same
    # scheduler: a day that is captured should also be verified and studied.
    init_tf_boost_daily_jobs()
    # A machine that sleeps through the open takes the scheduler with it and
    # does not bring it back; the watchdog does.
    init_tf_boost_watchdog()
