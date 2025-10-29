# --- Defaults / Vars ---
.ONESHELL:
SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV        ?= tradebot
HOSTNAME   := $(shell hostname)
LOGS       := logs
CSV        ?= data/ES_1m.csv
CSV_LIVE   ?= data/MES_live_1m.csv

# Unified runners
RUN  := conda run -n $(ENV) env PYTHONPATH=. python -u
RUNP := conda run -n $(ENV) env PYTHONPATH=.

# Common logs
EXEC_LOG   := $(LOGS)/executions.csv
OOS_LOG    := $(LOGS)/oos_log.csv
ALERTS_OUT := $(LOGS)/slack_alerts.out

.PHONY: help setup ensure-env rotate-logs alerts alerts-upload alerts-summary alerts-rebuild alerts-log charts watchdog analyze \
        notify-start notify-stop notify-health dash open start stop restart status oos-status health \
        backtest backtest-gui check report report-fast sweep data fetch-data live live-es live-mes clean status-dash

# -------------------- Core ops --------------------

help:
	@echo "make alerts             # run alerts (no auto-upload)"
	@echo "make alerts-upload      # run alerts and auto-upload charts"
	@echo "make alerts-summary     # alerts with --summary"
	@echo "make charts             # rebuild daily files and charts (no upload)"
	@echo "make alerts-rebuild     # rebuild + auto-upload charts"
	@echo "make status-dash        # quick terminal dashboard snapshot"
	@echo "make start|stop|status  # feeder + OOS loop control"
	@echo "make health             # health check wrapper"
	@echo "make backtest           # headless backtest on $(CSV)"
	@echo "make report             # backtest + open outputs"
	@echo "make sweep              # parameter sweep"
	@echo "make rotate-logs        # rotate/compress *.out logs"

setup: ensure-env
	conda run -n $(ENV) pip install --upgrade pip
	conda run -n $(ENV) pip install -r requirements.txt

ensure-env:
	@conda run -n $(ENV) python -c "import sys; sys.exit(0)" >/dev/null 2>&1 || \
	( echo "Conda env '$(ENV)' not found. Create it: conda create -n $(ENV) python=3.10 -y" && exit 1 )

rotate-logs:
	@find $(LOGS) -type f -name "*.out" -size +5M -exec mv {} {}.$(shell date +%Y%m%d) \;
	@gzip -f $(LOGS)/*.out.* || true

# -------------------- Alerts / Charts --------------------

alerts: ensure-env
	@mkdir -p $(LOGS)
	@echo ">> alerts $(ARGS)"
	@$(RUN) scripts/slack_alerts.py $(ARGS)

alerts-upload: ensure-env
	@mkdir -p $(LOGS)
	@echo ">> alerts (auto-upload) $(ARGS)"
	@UPLOAD_CHART=1 $(RUN) scripts/slack_alerts.py $(ARGS)

alerts-summary: ensure-env
	@mkdir -p $(LOGS)
	@echo ">> alerts --summary $(ARGS)"
	@$(RUN) scripts/slack_alerts.py --summary $(ARGS)

charts: ensure-env
	@$(RUN) scripts/slack_alerts.py --rebuild-daily
	@test -f $(LOGS)/daily_perf.png && echo "✓ daily_perf.png" || (echo "✗ daily_perf.png"; exit 1)
	@test -f $(LOGS)/streaks_history.png && echo "✓ streaks_history.png" || (echo "✗ streaks_history.png"; exit 1)

alerts-rebuild: ensure-env
	@UPLOAD_CHART=1 $(RUN) scripts/slack_alerts.py --rebuild-daily

alerts-log:
	@tail -n 200 $(ALERTS_OUT) || echo "No log yet."

# -------------------- Processes / Ops --------------------

watchdog:
	@./scripts/watchdog_feeder.sh 300

analyze: ensure-env
	@mkdir -p $(LOGS)
	@$(RUN) scripts/analyze_run.py
	@echo "---- live_summary.txt ----"
	@sed -n '1,120p' $(LOGS)/live_summary.txt || true

notify-start:
	@$(RUN) scripts/notify_slack.py "🚀 Tradebot STARTED on $(HOSTNAME). Feeder & OOS launching… (CSV=$(CSV_LIVE))"

notify-stop:
	@$(RUN) scripts/notify_slack.py "🛑 Tradebot STOPPED on $(HOSTNAME)."

notify-health:
	@out=$$(STRICT=1 STALE_SEC=1200 scripts/health_check.sh 2>&1); \
	echo "$$out"; \
	msg=$$(echo "$$out" | awk 'NR<=60' | sed 's/"/'\''/g'); \
	$(RUN) scripts/notify_slack.py "🩺 Tradebot Health on $(HOSTNAME): $${msg}"

dash: ensure-env
	@echo "Opening live dashboard at http://127.0.0.1:5055/"
	@$(RUN) scripts/live_dashboard.py --csv $(CSV_LIVE) --host 127.0.0.1 --port 5055

open:
	@bash scripts/tmux_watch.sh

start: ensure-env
	@mkdir -p $(LOGS)
	@nohup $(RUN) scripts/shadow_mes_delayed.py > $(LOGS)/shadow.out 2>&1 &
	@nohup $(RUN) scripts/run_oos_loop.py --cadence-min 10 --min-bars 150 \
	  --csv $(CSV_LIVE) --config config/overrides/clean_v1_be10_tick1.yaml \
	  > $(LOGS)/oos_loop.out 2>&1 &
	@$(MAKE) notify-start
	@echo "Started feeder + OOS loop"

stop:
	@pkill -f shadow_mes_delayed.py || true
	@pkill -f run_oos_loop.py || true
	@$(MAKE) notify-stop
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

# -------------------- Backtests / Reports --------------------

backtest: ensure-env
	@test -f "$(CSV)" || (echo "Missing $(CSV). Put ES 1m data there." && exit 1)
	@mkdir -p $(LOGS)
	@echo ">> backtest (headless) $(ARGS)"
	@MPLBACKEND=Agg $(RUN) run_backtest.py $(ARGS)

backtest-gui: ensure-env
	@test -f "$(CSV)" || (echo "Missing $(CSV). Put ES 1m data there." && exit 1)
	@mkdir -p $(LOGS)
	@$(RUN) run_backtest.py $(ARGS)

check: ensure-env
	@test -f "$(CSV)" || (echo "Missing $(CSV). Put ES 1m data there." && exit 1)
	@mkdir -p $(LOGS)
	@echo ">> smoke (headless)"
	@MPLBACKEND=Agg $(RUN) run_backtest.py --no-gui $(ARGS)
	@echo "----- summary.json -----"
	@python - <<'PY'
import json, sys
p = "logs/summary.json"
try:
    data = json.load(open(p))
except Exception as e:
    print(f"(no summary at {p})", e)
    sys.exit(1)
print(json.dumps(data, indent=2))
if not data.get("Trades", 0):
    sys.exit("ERROR: No trades in summary.json")
PY
	@echo "Smoke OK"

report: ensure-env
	@mkdir -p $(LOGS)
	@$(RUN) run_backtest.py --no-gui
	@test -f $(LOGS)/backtest_trades.csv && open $(LOGS)/backtest_trades.csv || true
	@test -f $(LOGS)/equity_curve.png && open $(LOGS)/equity_curve.png || true

report-fast: ensure-env
	@$(RUN) scripts/metrics_post.py
	@test -f $(LOGS)/backtest_trades.csv && open $(LOGS)/backtest_trades.csv || echo "No trades CSV yet."
	@test -f $(LOGS)/pnl_curve.png && open $(LOGS)/pnl_curve.png || echo "No PnL curve yet."

sweep: ensure-env
	@mkdir -p $(LOGS)
	@$(RUN) scripts/sweep.py
	@test -f $(LOGS)/sweep_results.csv && open $(LOGS)/sweep_results.csv || true

# -------------------- Data / Live --------------------

data: fetch-data
fetch-data: ensure-env
	@$(RUN) scripts/get_es_ib.py

live: ensure-env
	@mkdir -p $(LOGS)
	@$(RUN) run_live.py

live-es: ensure-env
	@echo ">> Live (paper) ES"
	@$(RUN) run_live.py --config config/settings.paper.yaml --symbol ES $(ARGS)

live-mes: ensure-env
	@echo ">> Live (paper) MES"
	@$(RUN) run_live.py --config config/settings.paper.yaml --symbol MES $(ARGS)

clean:
	@rm -f logs/*.csv state/*.json
	@echo "Cleaned logs/ and state/"

# -------------------- Convenience dashboard --------------------

status-dash: ensure-env
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
