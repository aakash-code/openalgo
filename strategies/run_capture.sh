#!/usr/bin/env bash
#
# Morning launcher for the 15-day paper test.
# Starts BOTH:
#   1) sector_capture.py    — guarantees the irreplaceable TradeFinder sector data
#   2) breakout strategy     — DRY_RUN paper trades + daily P&L summary
# Both auto-refresh the ~3h JWT via tf_auth (Playwright + persistent Google session).
#
# One-time first:  uv run python strategies/tf_login_setup.py   (sign into Google once)
#
# Usage:
#   OPENALGO_API_KEY=xxxxx ./strategies/run_capture.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root

: "${OPENALGO_API_KEY:?Set OPENALGO_API_KEY}"

# Recommended config (from this session's analysis) — override via env if you like.
export SECTOR_ONLY_MODE="${SECTOR_ONLY_MODE:-true}"
export SECTOR_MIN_BREADTH="${SECTOR_MIN_BREADTH:-65}"
export SECTOR_TOP_STOCKS_N="${SECTOR_TOP_STOCKS_N:-4}"
export SECTOR_MIN_RFACTOR="${SECTOR_MIN_RFACTOR:-0.5}"
export SECTOR_POLL_SEC="${SECTOR_POLL_SEC:-180}"   # capture sector scope every 3 min
export TARGET_RR="${TARGET_RR:-2.0}"
export TRAIL_STEPS_ENABLED="${TRAIL_STEPS_ENABLED:-false}"
export BREAKEVEN_START_R="${BREAKEVEN_START_R:-0}"
export USE_VWAP="${USE_VWAP:-false}"
export CAPITAL_PER_TRADE="${CAPITAL_PER_TRADE:-50000}"
export LEVERAGE="${LEVERAGE:-5}"
export MAX_OPEN_POSITIONS="${MAX_OPEN_POSITIONS:-15}"
# DRY_RUN=false routes orders through OpenAlgo's analyzer/sandbox (fake money, live fills,
# real order lifecycle visible in OpenAlgo's UI). The strategy's REQUIRE_ANALYZER guard
# refuses to start if analyzer mode is OFF, so this can never fire real broker orders.
# Set DRY_RUN=true to fall back to the strategy's internal simulation instead.
export DRY_RUN="${DRY_RUN:-false}"
export REQUIRE_ANALYZER="${REQUIRE_ANALYZER:-true}"

mkdir -p logs/breakout
DATE=$(date +%F)
CAP_LOG="logs/breakout/capture_daemon_${DATE}.log"
STR_LOG="logs/breakout/strategy_dryrun_${DATE}.log"

echo "Refreshing TradeFinder JWT (Playwright, headless)..."
uv run python -c "from strategies.tf_auth import refresh_tf_jwt; print('JWT:', 'ok' if refresh_tf_jwt() else 'FAILED — run tf_login_setup.py')" || true

# The DRY_RUN strategy runs its OWN sector poller — it captures sector_snapshots_DATE.jsonl
# AND paper-trades on live data. So for live testing we run JUST the strategy.
#
# The standalone capture daemon writes to the SAME snapshot file, so running both would
# double-write. Only enable the daemon as a cold backup on days you are NOT running the
# strategy (e.g. OpenAlgo/broker down):  RUN_CAPTURE_DAEMON=true ./strategies/run_capture.sh
if [[ "${RUN_CAPTURE_DAEMON:-false}" == "true" ]]; then
    echo "Starting standalone sector capture daemon (backup mode) → ${CAP_LOG}"
    nohup uv run python strategies/sector_capture.py >"${CAP_LOG}" 2>&1 &
    echo "  PID $!"
else
    echo "Strategy self-captures sector data — standalone daemon skipped (set RUN_CAPTURE_DAEMON=true to force)."
fi

echo "Starting strategy LIVE in DRY_RUN → ${STR_LOG}"
nohup uv run python strategies/breakout_intraday_strategy.py >"${STR_LOG}" 2>&1 &
echo "  PID $!"

echo ""
echo "Watch live decisions (when/where/why each trade fires):"
echo "  tail -f ${STR_LOG}"
echo ""
echo "Reminder: OpenAlgo must be running and Upstox logged in for live market data."
echo "After 15 trading days:  uv run python strategies/sector_report.py --capital 300000"
