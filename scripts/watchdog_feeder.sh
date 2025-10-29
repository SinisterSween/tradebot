#!/bin/bash
set -euo pipefail
CSV="data/MES_live_1m.csv"
STALE_SEC="${1:-300}"

if [[ ! -f "$CSV" ]]; then
  echo "[WD] no CSV; restarting feeder"
  pkill -f shadow_mes_delayed.py || true
  nohup python scripts/shadow_mes_delayed.py > logs/shadow.out 2>&1 &
  exit 0
fi

age=$(( $(date +%s) - $(stat -f %m "$CSV") ))
if (( age > STALE_SEC )); then
  echo "[WD] stale CSV ($age s); restarting feeder"
  pkill -f shadow_mes_delayed.py || true
  nohup python scripts/shadow_mes_delayed.py > logs/shadow.out 2>&1 &
else
  echo "[WD] fresh CSV ($age s)"
fi
