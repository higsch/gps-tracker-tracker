#!/usr/bin/env bash
#
# Install the launchd agent that runs `gtt poll` on a schedule.
#
# Override the defaults through the environment, e.g.
#   GTT_POLL_INTERVAL=60 scripts/install-launchd.sh
#
set -euo pipefail

LABEL="com.higsch.gps-tracker-tracker"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$PROJECT_DIR/scripts/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

INTERVAL="${GTT_POLL_INTERVAL:-300}"
DB_PATH="${GTT_DB_PATH:-$PROJECT_DIR/data/tracker.duckdb}"
CREDENTIALS_PATH="${GTT_CREDENTIALS_PATH:-$HOME/.config/gps-tracker-tracker/credentials.json}"

# launchd does not expand ~, and a relative DB path would resolve against the
# agent's working directory rather than yours.
DB_PATH="${DB_PATH/#\~/$HOME}"
CREDENTIALS_PATH="${CREDENTIALS_PATH/#\~/$HOME}"
case "$DB_PATH" in /*) ;; *) DB_PATH="$PROJECT_DIR/${DB_PATH#./}" ;; esac

UV_BIN="$(command -v uv || true)"
if [ ! -x "$UV_BIN" ]; then
    echo "error: uv is not on PATH. Install it with:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

if [ ! -f "$CREDENTIALS_PATH" ]; then
    echo "error: no credentials at $CREDENTIALS_PATH" >&2
    echo "  run \`uv run gtt login\` first, otherwise every poll will fail." >&2
    exit 1
fi

mkdir -p "$PROJECT_DIR/logs" "$(dirname "$DB_PATH")" "$HOME/Library/LaunchAgents"

sed -e "s|__UV_BIN__|$UV_BIN|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__INTERVAL__|$INTERVAL|g" \
    -e "s|__DB_PATH__|$DB_PATH|g" \
    -e "s|__CREDENTIALS_PATH__|$CREDENTIALS_PATH|g" \
    "$TEMPLATE" >"$TARGET"

plutil -lint "$TARGET" >/dev/null

# bootout first so re-running this script picks up changes.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/$LABEL"

cat <<EOF
Installed $LABEL
  plist        $TARGET
  every        ${INTERVAL}s
  database     $DB_PATH
  credentials  $CREDENTIALS_PATH
  log          $PROJECT_DIR/logs/poller.log

Poll now:   launchctl kickstart -p $DOMAIN/$LABEL
Status:     launchctl print $DOMAIN/$LABEL | head -20
Remove:     launchctl bootout $DOMAIN/$LABEL
EOF
