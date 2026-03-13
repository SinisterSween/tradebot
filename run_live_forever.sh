#!/usr/bin/env bash
# run_live_forever.sh — auto-restarts the trading bot on crash.
# Usage:  bash run_live_forever.sh [--dry-run]
#   To run in background and keep logs:
#   nohup bash run_live_forever.sh >> logs/tradebot.log 2>&1 &

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
ARGS="--portfolio mixed_1k $*"
RESTART_DELAY=15   # seconds to wait before restarting after a crash
MAX_RESTARTS=50    # safety cap — stop after this many consecutive crashes
restarts=0

echo "[WATCHDOG] Starting tradebot. Ctrl-C to stop."
echo "[WATCHDOG] Python: $PYTHON"
echo "[WATCHDOG] Args: $ARGS"

while true; do
    echo "[WATCHDOG] $(date '+%Y-%m-%d %H:%M:%S') — launching (restart #$restarts)"
    set +e
    "$PYTHON" "$SCRIPT_DIR/run_live.py" $ARGS
    exit_code=$?
    set -e

    if [ $exit_code -eq 0 ]; then
        echo "[WATCHDOG] Bot exited cleanly (code 0) — not restarting."
        break
    fi

    restarts=$((restarts + 1))
    if [ $restarts -ge $MAX_RESTARTS ]; then
        echo "[WATCHDOG] Reached $MAX_RESTARTS consecutive restarts — stopping."
        break
    fi

    echo "[WATCHDOG] Bot crashed (code $exit_code). Restarting in ${RESTART_DELAY}s... (restart $restarts/$MAX_RESTARTS)"
    sleep $RESTART_DELAY
done
