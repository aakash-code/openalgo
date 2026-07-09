#!/usr/bin/env bash
#
# Install the launchd agent that auto-starts the paper-test capture at 9:10 AM IST
# on weekdays. Fills the template with your API key + paths and loads it.
#
# Usage:
#   ./strategies/launchd/install.sh YOUR_OPENALGO_API_KEY
#
# Manage afterwards:
#   launchctl list | grep openalgo                 # is it loaded?
#   launchctl start in.openalgo.sector-capture     # run once now (test)
#   launchctl bootout gui/$(id -u)/in.openalgo.sector-capture   # uninstall
#
set -euo pipefail

API_KEY="${1:-}"
if [[ -z "$API_KEY" ]]; then
    echo "Usage: $0 YOUR_OPENALGO_API_KEY"
    exit 1
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
UV_DIR="$(dirname "$UV_BIN")"
TEMPLATE="$REPO/strategies/launchd/in.openalgo.sector-capture.plist.template"
LABEL="in.openalgo.sector-capture"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs/breakout"

sed -e "s|__API_KEY__|$API_KEY|g" \
    -e "s|__REPO__|$REPO|g" \
    -e "s|__UVDIR__|$UV_DIR|g" \
    "$TEMPLATE" > "$DEST"
chmod 600 "$DEST"   # contains the API key — keep it private

# Reload (bootout if already present, then bootstrap)
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"

echo "Installed: $DEST"
echo "Loaded as: $LABEL  (fires Mon–Fri 09:10 IST)"
echo ""
echo "Test it now without waiting for 9:10:"
echo "  launchctl start $LABEL"
echo "  tail -f $REPO/logs/breakout/strategy_dryrun_\$(date +%F).log"
echo ""
echo "Prerequisite (one-time): uv run python strategies/tf_login_setup.py"
