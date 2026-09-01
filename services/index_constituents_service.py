# services/index_constituents_service.py
"""
Index Constituents Sync Service
Fetches NSE sectoral index membership from niftyindices.com's free CSVs and
merges in weights from a manually-maintained factsheet-transcribed seed file
(data/index_weights.json). Weights change semi-annually (confirmed on the
factsheets themselves: "Index Rebalancing: Semi-Annually"), so this runs
monthly via APScheduler, not on a live/real-time cadence.
"""

import json
import os
import time
from datetime import date, datetime
from io import StringIO

import requests

from database.index_constituents_db import (
    db_session,
    replace_index_version,
    set_sync_status,
)
from database.symbol import SymToken
from utils.logging import get_logger

logger = get_logger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
WEIGHTS_SEED_PATH = os.path.join(DATA_DIR, "index_weights.json")

# index_symbol (as subscribed on NSE_INDEX) -> niftyindices.com CSV slug.
# Slugs verified against the "niftyindices.com/IndexConstituent/ind_<slug>list.csv"
# pattern found during research; membership-only, no weights in this file.
INDEX_CSV_SLUGS = {
    "BANKNIFTY": "niftybank",
    "NIFTYIT": "niftyit",
    "NIFTYAUTO": "niftyauto",
    "NIFTYFMCG": "niftyfmcg",
    "NIFTYMETAL": "niftymetal",
    "NIFTYPHARMA": "niftypharma",
    "NIFTYREALTY": "niftyrealty",
    "NIFTYMEDIA": "niftymedia",
    "NIFTYPSUBANK": "niftypsubank",
    "NIFTYPVTBANK": "nifty_privatebank",  # note: underscored, unlike its siblings above
    # Real broker index_symbol (verified against the live symtoken table,
    # NSE_INDEX exchange) has spaces, unlike every sibling above -- using
    # "NIFTYOILGAS" here would silently break the live index-level WS
    # subscription (no such NSE_INDEX symbol exists).
    "NIFTY OIL AND GAS": "niftyoilgas",
    # NIFTYPOWER intentionally omitted from the whole feature, not just this
    # map: verified against the live symtoken table that no NSE_INDEX symbol
    # for it exists yet (it's a newly-launched index per its factsheet, June
    # 2026) -- there is no live index level to subscribe to regardless of
    # constituent data, so it cannot function in the real-time engine. Add
    # back once a broker actually lists it.
    #
    # NIFTYENERGY: confirmed to exist as a live NSE_INDEX symbol, but no
    # factsheet weight data has been transcribed for it yet -- deliberately
    # left out rather than guessing weights. Add once its factsheet is
    # sourced and added to data/index_weights.json.
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _load_weight_seed() -> dict:
    try:
        with open(WEIGHTS_SEED_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"[IndexConstituentsService] No weight seed found at {WEIGHTS_SEED_PATH}")
        return {"as_of": None, "weights": {}}
    except Exception:
        logger.exception("[IndexConstituentsService] Error loading weight seed")
        return {"as_of": None, "weights": {}}


def _fetch_membership_csv(slug: str) -> list[str] | None:
    """Return the list of EQ-series symbols for one index, or None on failure
    (caller keeps the existing DB rows rather than wiping them on a blocked
    scrape)."""
    url = f"https://niftyindices.com/IndexConstituent/ind_{slug}list.csv"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        import csv

        reader = csv.DictReader(StringIO(resp.text))
        symbols = [
            row["Symbol"].strip()
            for row in reader
            if row.get("Series", "").strip() == "EQ" and row.get("Symbol")
        ]
        return symbols or None
    except Exception as e:
        logger.warning(f"[IndexConstituentsService] Membership fetch failed for {slug}: {e}")
        return None


def _resolve_symbol(symbol: str) -> bool:
    return SymToken.query.filter_by(symbol=symbol, exchange="NSE").first() is not None


def sync_index_constituents(force: bool = False) -> dict:
    """
    Sync membership (from CSV) + weights (from the seed file) for every index
    in INDEX_CSV_SLUGS. Per-index failures are logged and that index's existing
    rows are left untouched -- a blocked scrape on one index must never wipe
    working data for the others.

    All index writes are staged (commit=False) and committed together in ONE
    transaction at the end, not one commit per index. openalgo.db is shared by
    every feature module plus the out-of-process websocket proxy, and the
    project's global busy_timeout is 15s (database/__init__.py) -- 11+ separate
    commits fired back-to-back during the app-boot write storm exceeded that
    and raised a real "database is locked" error during validation. One commit
    means one write-lock acquisition instead of eleven.
    """
    from sqlalchemy.exc import OperationalError

    set_sync_status("running", "Sync started")
    seed = _load_weight_seed()
    seed_weights = seed.get("weights", {})
    weights_as_of = (
        datetime.strptime(seed["as_of"], "%Y-%m-%d").date() if seed.get("as_of") else date.today()
    )

    stats = {}
    for index_symbol, slug in INDEX_CSV_SLUGS.items():
        try:
            members = _fetch_membership_csv(slug)
            if members is None:
                stats[index_symbol] = {"status": "fetch_failed", "n": None}
                continue

            resolved = [s for s in members if _resolve_symbol(s)]
            unresolved = set(members) - set(resolved)
            if unresolved:
                logger.warning(
                    f"[IndexConstituentsService] {index_symbol}: "
                    f"{len(unresolved)} symbols not found in symtoken (NSE): {sorted(unresolved)}"
                )

            known_weights = seed_weights.get(index_symbol, {})
            named = {s: w for s, w in known_weights.items() if s in resolved}
            unnamed = [s for s in resolved if s not in named]
            remainder = max(0.0, 100.0 - sum(named.values()))
            equal_share = remainder / len(unnamed) if unnamed else 0.0

            constituents = [
                {"symbol": s, "exchange": "NSE", "weight": w, "weight_source": "factsheet"}
                for s, w in named.items()
            ] + [
                {"symbol": s, "exchange": "NSE", "weight": equal_share, "weight_source": "equal"}
                for s in unnamed
            ]

            total_weight = sum(c["weight"] for c in constituents)
            if abs(total_weight - 100.0) > 2.0:
                logger.warning(
                    f"[IndexConstituentsService] {index_symbol}: weights sum to "
                    f"{total_weight:.2f}, expected ~100 -- check the seed file"
                )

            # SAVEPOINT per index: replace_index_version(commit=False) stages
            # rows on the shared session without writing to disk. If THIS
            # index blows up partway through staging, only its own savepoint
            # rolls back -- a plain db_session.rollback() here would also
            # discard every earlier index already staged in this same pass.
            nested = db_session.begin_nested()
            try:
                replace_index_version(
                    index_symbol=index_symbol,
                    index_exchange="NSE_INDEX",
                    constituents=constituents,
                    effective_from=date.today(),
                    commit=False,
                )
                nested.commit()  # releases the savepoint; outer transaction still open
            except Exception:
                nested.rollback()
                raise

            stats[index_symbol] = {
                "status": "ok",
                "n": len(constituents),
                "weighted": round(total_weight, 2),
                "unresolved": len(unresolved),
            }
        except Exception:
            logger.exception(f"[IndexConstituentsService] Sync failed for {index_symbol}")
            stats[index_symbol] = {"status": "error", "n": None}

    # One commit for the whole batch (see docstring). Transient SQLite lock
    # contention is expected at boot (openalgo.db is shared by every module
    # plus the out-of-process websocket proxy) -- retry a few times with
    # backoff before giving up, rather than surfacing a spurious failure for
    # what the project's own busy_timeout comment calls a normal, recoverable
    # queueing delay.
    committed = False
    last_error = None
    for attempt in range(3):
        try:
            db_session.commit()
            committed = True
            break
        except OperationalError as e:
            last_error = e
            db_session.rollback()
            logger.warning(
                f"[IndexConstituentsService] Commit attempt {attempt + 1}/3 hit a locked "
                f"database, retrying: {e}"
            )
            time.sleep(1.5 * (attempt + 1))

    if not committed:
        logger.error(f"[IndexConstituentsService] Final commit failed after retries: {last_error}")
        for s in stats.values():
            if s["status"] == "ok":
                s["status"] = "error"  # staged but never actually persisted
        set_sync_status(
            "error",
            message="Database locked -- sync produced no changes, will retry next cycle",
            index_stats=json.dumps(stats),
        )
        return stats

    ok_count = sum(1 for s in stats.values() if s["status"] == "ok")
    set_sync_status(
        "success" if ok_count else "error",
        message=f"{ok_count}/{len(INDEX_CSV_SLUGS)} indices synced",
        index_stats=json.dumps(stats),
        weights_as_of=weights_as_of,
    )
    return stats


def apply_manual_override(index_symbol: str, constituents: list[dict]) -> bool:
    """
    Escape hatch for when niftyindices.com is blocked mid-cycle: write a
    weight_source='manual' version directly, bypassing the scrape entirely.
    `constituents` is [{symbol, weight}, ...].
    """
    rows = [
        {
            "symbol": c["symbol"],
            "exchange": c.get("exchange", "NSE"),
            "weight": c.get("weight"),
            "weight_source": "manual",
        }
        for c in constituents
    ]
    return replace_index_version(
        index_symbol=index_symbol,
        index_exchange="NSE_INDEX",
        constituents=rows,
        effective_from=date.today(),
    )


def init_index_constituents_scheduler():
    """
    Registers a monthly sync job. Uses a plain in-memory BackgroundScheduler
    rather than a persisted-jobstore singleton (unlike Flow/Historify): losing
    the exact fire time across a restart is harmless here since weights change
    semi-annually, and the cold-start sync below already covers a fresh install.
    ponytail: in-memory scheduler, move to a persisted jobstore (like
    FLOW_JOBSTORE_TABLE) if this job ever needs restart-survivable retries.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    from database.index_constituents_db import get_supported_indices

    scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1})
    scheduler.add_job(
        sync_index_constituents,
        CronTrigger(day=1, hour=6, minute=30, timezone="Asia/Kolkata"),
        id="index_constituents_sync",
        replace_existing=True,
    )
    scheduler.start()
    logger.debug("[IndexConstituentsService] Monthly sync scheduler started")

    if not get_supported_indices():
        # Deferred, not run inline here: this function executes inside
        # app.py's post-db-init scheduler block, in the same window where
        # every other feature (Flow, Historify, price alerts, order-update
        # watches) is also writing to the shared openalgo.db. Running the
        # 11-index sync synchronously in that window is what produced a real
        # "database is locked" failure during validation, even with the
        # per-index writes batched into one commit. A 45s delay clears the
        # boot storm; the job still fires exactly once (coalesce=True caps
        # any pile-up to a single run if start() is somehow called twice).
        from datetime import timedelta

        from apscheduler.triggers.date import DateTrigger

        logger.info(
            "[IndexConstituentsService] No constituent data found, "
            "scheduling cold-start sync 45s from now"
        )
        scheduler.add_job(
            sync_index_constituents,
            DateTrigger(run_date=datetime.now() + timedelta(seconds=45)),
            id="index_constituents_cold_start",
            kwargs={"force": True},
            replace_existing=True,
        )
