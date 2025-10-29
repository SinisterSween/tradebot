#!/bin/bash
set -e
export PATH="/Users/hatefulsween/miniforge3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/hatefulsween/code/tradebot || exit 1
echo "RUNNING: $0  $(date)" >> logs/alerts.log
echo "PATH=$PATH" >> logs/alerts.log
./scripts/watchdog_feeder.sh 300 >> logs/watchdog.out 2>&1 || true
/Users/hatefulsween/miniforge3/bin/conda run -n tradebot python -u scripts/slack_alerts.py >> logs/alerts.log 2>&1
