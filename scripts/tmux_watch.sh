#!/usr/bin/env bash
set -euo pipefail

SESSION="${SESSION:-tradebot-watch}"
CSV="${CSV:-data/MES_live_1m.csv}"
SHADOW="${SHADOW:-logs/shadow.out}"
OOS="${OOS:-logs/oos_loop.out}"

# Start/attach a tmux session with 3 panes:
# - left: CSV tail -f (last 25 lines)
# - top-right: feeder log
# - bottom-right: OOS loop log

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Attaching to existing tmux session: $SESSION"
  exec tmux attach -t "$SESSION"
fi

tmux new-session -d -s "$SESSION" "bash -lc 'echo CSV: $CSV; tail -n 25 -f "$CSV"'"
tmux split-window -h "bash -lc 'echo FEEDER LOG: $SHADOW; tail -n 50 -f "$SHADOW"'"
tmux split-window -v "bash -lc 'echo OOS LOG: $OOS; tail -n 50 -f "$OOS"'"
tmux select-layout even-horizontal
tmux select-pane -L

echo "Started tmux session: $SESSION"
echo "Tip: detach with Ctrl-b d"
exec tmux attach -t "$SESSION"
