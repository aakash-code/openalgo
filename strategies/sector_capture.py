#!/usr/bin/env python3
"""
Standalone TradeFinder Sector-Scope capture daemon.

The ONLY job: every SECTOR_POLL_SEC during market hours, fetch the TF sector scope
and append a snapshot to logs/breakout/sector_snapshots_YYYY-MM-DD.jsonl. This data
has NO history API on TradeFinder's side — if it isn't captured live, the day is gone.

Runs independently of the broker / OpenAlgo, so it keeps capturing even if trading is
down. Auto-refreshes the ~3h JWT via tf_auth (Playwright + persistent Google session).

Usage:
    uv run python strategies/sector_capture.py            # run until market close
    SECTOR_POLL_SEC=300 uv run python strategies/sector_capture.py
"""

import os
import signal
import sys
import time

# Import the strategy module (reuses fetch + snapshot-build + session constants).
import breakout_intraday_strategy as B
import tf_auth

_stop = False


def _handle_sigterm(*_):
    global _stop
    _stop = True
    print("[capture] SIGTERM — stopping after current poll")


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    poll = B.SECTOR_POLL_SEC
    print("=" * 64)
    print("  SECTOR CAPTURE DAEMON")
    print(f"  poll={poll}s  long={B.SECTOR_TOP_N_LONG} short={B.SECTOR_TOP_N_SHORT} "
          f"breadth>={B.SECTOR_MIN_BREADTH:.0f}% top_stocks={B.SECTOR_TOP_STOCKS_N}")
    print(f"  output: {B._SECTOR_SNAPSHOT_FILE}")
    print("=" * 64)

    prev_scores: dict = {}
    polls = 0

    while not _stop:
        cm = B._cur_min()
        # Pre-market: idle wait until open
        if cm < B.SESSION_START_MIN:
            wait = min(60, (B.SESSION_START_MIN - cm) * 60)
            print(f"[{B._now_ist():%H:%M:%S}] pre-market, waiting {wait}s")
            time.sleep(wait)
            continue
        # Post-market: done for the day
        if cm > B.SESSION_END_MIN:
            print(f"[{B._now_ist():%H:%M:%S}] market closed — captured {polls} snapshots. Exit.")
            break

        # Keep the JWT fresh (refresh via browser when <30 min left). Hot-reloads tf_jwt.txt
        # which fetch_sector_scope() reads on every call.
        try:
            tf_auth.ensure_fresh_jwt(min_seconds=1800)
        except Exception as e:
            print(f"[capture] jwt refresh error (continuing on file token): {e}")

        sector_data = B.fetch_sector_scope()
        ts = B._now_ist().strftime("%H:%M")
        if not sector_data:
            print(f"[{ts}] empty sector data (JWT expired / weekend?) — skipping")
        else:
            snap = B.build_sector_snapshot(sector_data, ts=ts, prev_scores=prev_scores)
            B._save_sector_snapshot(snap)
            prev_scores = snap["sector_scores"]
            polls += 1
            longs  = snap["long_sectors"]
            shorts = snap["short_sectors"]
            print(f"[{ts}] snapshot #{polls}  LONG={longs}  SHORT={shorts}  "
                  f"breadth={snap['sector_breadth']}")

        # Sleep in 5s slices so SIGTERM is responsive
        slept = 0
        while slept < poll and not _stop:
            time.sleep(min(5, poll - slept))
            slept += 5

    return 0


if __name__ == "__main__":
    sys.exit(main())
