#!/bin/sh
# Unattended entrypoint (Linux / macOS / WSL / GitHub Actions / n8n on a server).
# Run as `sh run_radar.sh [auto|daily|digest]` so the executable bit does not matter.
#
# - First run writes a baseline and sends nothing (any mode).
# - Quiet days exit 0 and send nothing.
# - Monday UTC (mode auto) sends the weekly digest; other days send only a real delta.
# - Telegram is used only when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both set.
#
# Secrets and settings come from the environment. If a radar.env file exists next to
# this script (or RADAR_ENV_FILE points to one), it is loaded first. Never commit it.
#
# State file: $RADAR_STATE if set; otherwise <repo root>/state/telegram-radar.json,
# where repo root is two levels above this script (tools/telegram-python-now/ layout).
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
ENV_FILE="${RADAR_ENV_FILE:-$SCRIPT_DIR/radar.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
if [ -n "${RADAR_STATE:-}" ]; then
  STATE="$RADAR_STATE"
else
  ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
  cd "$ROOT"
  STATE="$ROOT/state/telegram-radar.json"
fi
exec python3 "$SCRIPT_DIR/radar_diff.py" run --state "$STATE" --mode "${1:-auto}"

