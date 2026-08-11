"""TF JWT keep-alive service.

The server-side TradeFinder JWT (strategies/tf_jwt.txt) is what the
tf_boost_snapshot_service and Python TF strategies authenticate with. Nothing
was keeping it fresh day-to-day — it only gets renewed if a strategy script
happens to be running, or someone manually pastes a token. Refreshing it via
strategies/tf_auth.py's browser automation is slow (up to ~60s, launches a
headless Chromium against a persisted, already-logged-in Google profile), so
it must never run inline in a request thread.

This service is designed to be pinged frequently and cheaply by an ACTIVE
openalgo-chart session (the user has the chart open regularly, per their own
usage — see useTradeFinderBoost.ts): a cheap expiry check gates the slow
refresh, which runs in a background thread so the HTTP response returns
immediately regardless of whether a refresh was kicked off.
"""

from __future__ import annotations

import importlib.util
import os
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from utils.logging import get_logger

logger = get_logger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TF_AUTH_PATH = os.path.join(_REPO_ROOT, "strategies", "tf_auth.py")

_tf_auth = None
_lock = threading.Lock()
_refreshing = False

_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()
_KEEPALIVE_JOB_ID = "tf_jwt_keepalive_tick"


def _load_tf_auth():
    """strategies/ has no __init__.py (its scripts run standalone via `uv run`
    from that directory), so this is loaded by file path rather than imported
    as a package — same reason services/tradefinder_service.py re-implements
    rather than imports its strategies/ counterpart."""
    global _tf_auth
    if _tf_auth is None:
        spec = importlib.util.spec_from_file_location("tf_auth_dynamic", _TF_AUTH_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _tf_auth = module
    return _tf_auth


def get_tf_jwt_status() -> dict:
    ta = _load_tf_auth()
    token = ta._read_file_jwt()
    return {
        "hasToken": bool(token),
        "expiresInSeconds": max(0, int(ta.jwt_expiry_seconds(token))) if token else 0,
        "refreshing": _refreshing,
    }


def trigger_refresh_if_needed(min_seconds: int = 1800) -> dict:
    """Non-blocking. Cheap file read + expiry check; if the token is missing or
    expiring within `min_seconds`, kicks off the browser refresh in a
    background thread (deduped — a refresh already in flight is not
    re-launched) and returns immediately. Safe to call every few minutes."""
    global _refreshing
    ta = _load_tf_auth()
    token = ta._read_file_jwt()
    needs_refresh = not token or ta.jwt_expiry_seconds(token) <= min_seconds

    if needs_refresh:
        with _lock:
            if not _refreshing:
                _refreshing = True

                def _run():
                    global _refreshing
                    try:
                        # refresh_tf_jwt reports its own failures on stdout and
                        # returns None rather than raising, so without this the
                        # service logged nothing and a dead refresh looked
                        # identical to a healthy one — the token simply expired
                        # with no trace in log/errors.jsonl.
                        if not ta.refresh_tf_jwt():
                            logger.error(
                                "tf_jwt_keepalive: refresh produced no token. Check the "
                                "browser profile is still logged in (uv run python "
                                "strategies/tf_login_setup.py) and that the Playwright "
                                "chromium build is installed (uv run playwright install chromium)."
                            )
                    except Exception as e:
                        logger.warning(f"tf_jwt_keepalive: background refresh failed: {e}")
                    finally:
                        _refreshing = False

                threading.Thread(target=_run, daemon=True, name="tf-jwt-refresh").start()
                logger.info("tf_jwt_keepalive: token missing/expiring, background refresh started")

    return get_tf_jwt_status()


def init_tf_jwt_keepalive_scheduler(interval_minutes: int = 20) -> None:
    """Server-native keep-alive: runs independent of any chart tab, so the
    server-side TF JWT self-heals on its own cadence whenever OpenAlgo itself
    is running — including right after a restart, when the token may have
    gone stale while the machine was off. The chart's own /tfjwtkeepalive
    pings (useTradeFinderBoost.ts) and this scheduler both funnel through
    trigger_refresh_if_needed's single _refreshing guard, so they can never
    launch two overlapping browser refreshes.

    Idempotent; safe to call once at app startup."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            logger.debug("tf_jwt_keepalive: scheduler already initialized, skipping")
            return

        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            lambda: trigger_refresh_if_needed(min_seconds=1800),
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=_KEEPALIVE_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )
        _scheduler.start()

        # Fire once immediately at boot too, so a restart self-heals right
        # away instead of waiting a full interval — this is what covers the
        # "computer was off overnight, token went stale" case.
        threading.Thread(
            target=lambda: trigger_refresh_if_needed(min_seconds=1800),
            daemon=True,
            name="tf-jwt-keepalive-boot",
        ).start()

        logger.info(
            f"TF JWT keep-alive scheduler started (every {interval_minutes} min, "
            "independent of any client — self-heals on server restart)"
        )
