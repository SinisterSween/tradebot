# --- Minimal Tradebot Makefile ---
.ONESHELL:
SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV        ?= tradebot
HOSTNAME   := $(shell hostname)
LOGS       := logs
CSV_LIVE   ?= data/MES_live_1m.csv

# Unified runner with PYTHONPATH so imports work everywhere
RUN := conda run -n $(ENV) env PYTHONPATH=. python -u

# Common logs
OOS_LOG    := $(LOGS)/oos_log.csv
ALERTS_OUT := $(LOGS)/slack_alerts.out

.PHONY: help setup ensure-env rotate-logs alerts alerts-upload alerts-summary charts \
        start stop restart status oos-status health clean status-dash

help:
	@echo "make alerts            # run alerts (no auto-upload)"
	@echo "make alerts-upload     # alerts with auto-upload"
	@echo "make alerts-summary    # alerts --summary"
	@echo "make charts            # rebuild daily files/charts (no upload)"
	@echo "make start|stop|status # feeder + OOS control"
	@echo "make oos-status        # tail OOS logs"
	@echo "make health            # feed + loop health check"
	@echo "make status-dash       # compact terminal dashboard"
	@echo "make rotate-logs       # rotate/compress *.out logs"
	@echo "make clean             # purge generated csv/json (safe)"

setup: ensure-env
	conda run -n $(ENV) pip install --upgrade pip
	conda run -n $(ENV) pip install -r requirements.txt

ensure-env:
	@conda run -n $(ENV) python -c "import sys; sys.exit(0)" >/dev/null 2>&1 || \
	( echo "Conda env '$(ENV)' not found. Create it: conda create -n $(ENV) python=3.10 -y" && exit 1 )

rotate-logs:
	@find $(LOGS) -type f -name "*.out" -size +5M -exec mv {} {}.$(shell date +%Y%m%d) \;
	@gzip -f $(LOGS)/*.out.* || true

# -------- Alerts / Charts --------

alerts:
	@mkdir -p $(LOGS)
	@echo ">> alerts $(ARGS)"
	@$(RUN) scripts/slack_alerts.py $(ARGS)

alerts-upload:
	@mkdir -p $(LOGS)
	@echo ">> alerts (auto-upload) $(ARGS)"
	@UPLOAD_CHART=1 $(RUN) scripts/slack_alerts.py $(ARGS)

alerts-summary:
	@mkdir -p $(LOGS)
	@echo ">> alerts --summary $(ARGS)"
	@$(RUN) scripts/slack_alerts.py --summary $(ARGS)

charts:
	@$(RUN) scripts/slack_alerts.py --rebuild-daily
	@test -f $(LOGS)/daily_perf.png && echo "✓ daily_perf.png" || (echo "✗ daily_perf.png"; exit 1)
	@test -f $(LOGS)/streaks_history.png && echo "✓ streaks_history.png" || (echo "✗ streaks_history.png"; exit 1)

# -------- Processes / Ops --------

start:
	@mkdir -p $(LOGS)
	@nohup $(RUN) scripts/shadow_mes_delayed.py > $(LOGS)/shadow.out 2>&1 &
	@nohup $(RUN) scripts/run_oos_loop.py --cadence-min 10 --min-bars 150 \
	  --csv $(CSV_LIVE) --config config/overrides/clean_v1_be10_tick1.yaml \
	  > $(LOGS)/oos_loop.out 2>&1 &
	@$(RUN) scripts/notify_slack.py "🚀 Tradebot STARTED on $(HOSTNAME)."
	@echo "Started feeder + OOS loop"

stop:
	@pkill -f shadow_mes_delayed.py || true
	@pkill -f run_oos_loop.py || true
	@$(RUN) scripts/notify_slack.py "🛑 Tradebot STOPPED on $(HOSTNAME)."
	@echo "Stopped feeder + OOS loop"

restart: stop start

status:
	@echo "PIDs:"; pgrep -fl shadow_mes_delayed.py || echo "feeder: none"; \
	pgrep -fl run_oos_loop.py || echo "oos: none"; \
	echo ""; echo "Last bar:"; tail -n 1 $(CSV_LIVE) || true; \
	echo ""; echo "Last OOS row:"; tail -n 1 $(OOS_LOG) || true

oos-status:
	@pgrep -fl run_oos_loop.py || echo "oos: none"
	@tail -n 20 $(LOGS)/oos_loop.out || true
	@tail -n 3  $(OOS_LOG) || true

health:
	@echo ">> Health (STRICT=1, STALE_SEC=1200)"
	@STRICT=1 STALE_SEC=1200 bash scripts/health_check.sh

clean:
	@rm -f logs/*.csv state/*.json
	@echo "Cleaned logs/ and state/"

# -------- Convenience dashboard --------

status-dash:
	@echo "=== Tradebot Status @ $(HOSTNAME) ==="
	@date
	@echo ""
	@$(MAKE) -s status
	@echo ""
	@echo "--- Latest Alerts (summary) ---"
	@$(RUN) scripts/slack_alerts.py --summary | tail -n 20 || true
	@echo ""
	@echo "--- OOS Tail ---"
	@tail -n 5 $(OOS_LOG) || true
	@echo ""
	@echo "--- streaks.txt ---"
	@sed -n '1,20p' logs/streaks.txt || true
