# --- Tradebot Makefile ---
.ONESHELL:
SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---- Env / Paths ----
ENV        ?= tradebot
HOSTNAME   := $(shell hostname)
LOGS       := logs
CSV_LIVE   ?= data/MES_live_1m.csv

# Unified runner with PYTHONPATH so imports work everywhere
RUN := conda run -n $(ENV) env PYTHONPATH=. python -u

# Common logs
OOS_LOG    := $(LOGS)/oos_log.csv
ALERTS_OUT := $(LOGS)/slack_alerts.out

.PHONY: \
	help setup ensure-env rotate-logs clean \
	start stop restart status status-dash oos-status health \
	start-all stop-all stop-live status-all \
	start-mes stop-mes status-mes alerts-mes \
	start-mnq stop-mnq status-mnq alerts-mnq alerts-upload-mnq alerts-summary-mnq \
	start-spy stop-spy start-qqq stop-qqq \
	alerts alerts-upload alerts-summary alerts-all alerts-rebuild charts \
	microlot-status help crypto-csv-90 crypto-csv-180 crypto-bt crypto-bt-window crypto-bt-window-friction \
    crypto-bt-2m crypto-bt-2m-friction bt-sol bt-eth

# -------- Help --------
help:
	@echo "make start-mes          # start MES feeder + OOS loop"
	@echo "make stop-mes           # stop MES feeder + OOS loop"
	@echo "make start-mnq          # start MNQ feeder + OOS loop"
	@echo "make stop-mnq           # stop MNQ feeder + OOS loop"
	@echo "make start-spy/stop-spy # start/stop SPY feeder + OOS loop"
	@echo "make start-qqq/stop-qqq # start/stop QQQ feeder + OOS loop"
	@echo "make start              # generic MES start (legacy)"
	@echo "make stop               # generic MES stop (legacy)"
	@echo "make start-all          # start MES/MNQ/SPY/QQQ"
	@echo "make stop-all           # kill all feeders + loops"
	@echo "make stop-live          # kill run_live.py portfolio runner"
	@echo "make alerts             # run alerts (no auto-upload)"
	@echo "make alerts-upload      # alerts with auto-upload"
	@echo "make alerts-summary     # alerts --summary"
	@echo "make charts             # rebuild daily files/charts"
	@echo "make status             # process + OOS status (MES/MNQ)"
	@echo "make status-dash        # compact terminal dashboard"
	@echo "make rotate-logs        # rotate/compress *.out logs"
	@echo "make clean              # purge generated csv/json (safe)"
	@echo ""
	@echo "=== Common targets ==="
	@echo "make crypto-csv-90                # build crypto CSVs (90d)"
	@echo "make crypto-csv-180               # build crypto CSVs (180d)"
	@echo "make crypto-bt                    # backtest crypto (full range in CSVs)"
	@echo "make crypto-bt-2m                 # backtest crypto for START_DATE..END_DATE"
	@echo "make crypto-bt-2m-friction        # same but with harsher friction (fee/slip)"
	@echo ""
	@echo "Override knobs:"
	@echo "  STARTING_EQUITY=2000 RISK_PCT=0.01 FEE_BPS=3 SLIP_BPS=2"
	@echo "  START_DATE=YYYY-MM-DD END_DATE=YYYY-MM-DD"
	@echo ""
	@echo "Examples:"
	@echo "  make crypto-bt-2m START_DATE=2025-11-01 END_DATE=2025-11-30"
	@echo "  make crypto-bt STARTING_EQUITY=5000 RISK_PCT=0.005"
	@echo ""

PY := .venv/bin/python
PP := PYTHONPATH=.
PROFILE_EQ := config/profile/equities.yaml
UNIV_CRYPTO := config/universes/crypto_micro.yaml
CSV_DIR := data
# Backtest defaults
STARTING_EQUITY ?= 2000
RISK_PCT ?= 0.01
FEE_BPS ?= 3.0
SLIP_BPS ?= 2.0
# Date window defaults (override on command line if you want)
# example: make crypto-bt-window START_DATE=2025-10-01 END_DATE=2025-11-30
START_DATE ?= 2025-10-01
END_DATE   ?= 2025-11-30

# --- CSV generation (adjust flags to match your script if needed) ---

crypto-csv-90:
	$(PP) $(PY) scripts/build_universe_csvs.py --universe $(UNIV_CRYPTO) --csv-dir $(CSV_DIR) --days 90

crypto-csv-180:
	$(PP) $(PY) scripts/build_universe_csvs.py --universe $(UNIV_CRYPTO) --csv-dir $(CSV_DIR) --days 180

# --- Backtests ---

# Full-range backtest (whatever dates are in the CSV)
crypto-bt:
	$(PP) $(PY) run_backtest.py \
	  --profile $(PROFILE_EQ) \
	  --universe $(UNIV_CRYPTO) \
	  --all \
	  --csv-dir $(CSV_DIR) \
	  --starting-equity $(STARTING_EQUITY) \
	  --risk-pct $(RISK_PCT) \
	  --no-gui

# Windowed backtest (uses your --start-date/--end-date args)
crypto-bt-window:
	$(PP) $(PY) run_backtest.py \
	  --profile $(PROFILE_EQ) \
	  --universe $(UNIV_CRYPTO) \
	  --all \
	  --csv-dir $(CSV_DIR) \
	  --starting-equity $(STARTING_EQUITY) \
	  --risk-pct $(RISK_PCT) \
	  --no-gui \
	  --start-date $(START_DATE) \
	  --end-date $(END_DATE)

# Same window but with friction
crypto-bt-window-friction:
	$(PP) $(PY) run_backtest.py \
	  --profile $(PROFILE_EQ) \
	  --universe $(UNIV_CRYPTO) \
	  --all \
	  --csv-dir $(CSV_DIR) \
	  --starting-equity $(STARTING_EQUITY) \
	  --risk-pct $(RISK_PCT) \
	  --no-gui \
	  --fee-bps $(FEE_BPS) \
	  --slip-bps $(SLIP_BPS) \
	  --start-date $(START_DATE) \
	  --end-date $(END_DATE)

# Your most common “2 month” run (just aliases the window targets)
crypto-bt-2m: crypto-bt-window

crypto-bt-2m-friction: crypto-bt-window-friction

# Optional: per-symbol backtests (if you ever want quick isolated runs)
bt-sol:
	$(PP) $(PY) run_backtest.py \
	  --profile $(PROFILE_EQ) \
	  --universe $(UNIV_CRYPTO) \
	  --symbol SOL/USDT \
	  --csv-dir $(CSV_DIR) \
	  --starting-equity $(STARTING_EQUITY) \
	  --risk-pct $(RISK_PCT) \
	  --no-gui \
	  --fee-bps $(FEE_BPS) \
	  --slip-bps $(SLIP_BPS) \
	  --start-date $(START_DATE) \
	  --end-date $(END_DATE)

bt-eth:
	$(PP) $(PY) run_backtest.py \
	  --profile $(PROFILE_EQ) \
	  --universe $(UNIV_CRYPTO) \
	  --symbol ETH/USDT \
	  --csv-dir $(CSV_DIR) \
	  --starting-equity $(STARTING_EQUITY) \
	  --risk-pct $(RISK_PCT) \
	  --no-gui \
	  --fee-bps $(FEE_BPS) \
	  --slip-bps $(SLIP_BPS) \
	  --start-date $(START_DATE) \
	  --end-date $(END_DATE)

# -------- Global stop / status --------

stop-all:
	@echo "Hard stopping all tradebot python procs..."
	@ps aux | egrep "run_oos_loop.py|shadow_mes_delayed.py" | egrep -v egrep | awk '{print $$2}' | xargs -I{} kill -9 {} 2>/dev/null || true
	@pkill -f "run_live.py" 2>/dev/null || true
	@$(RUN) scripts/notify_slack.py "🛑 All Stopped on $(HOSTNAME)."
	@echo "All stopped."

stop-live:
	@echo "Killing run_live.py processes..."
	@pkill -f "run_live.py" 2>/dev/null || echo "No run_live.py processes found"

status-all:
	@$(MAKE) -s status-mes
	@echo ""
	@$(MAKE) -s status-mnq

alerts-all: ensure-env
	@conda run -n $(ENV) env PYTHONPATH=. SYMBOL=MES python -u scripts/slack_alerts.py --summary
	@conda run -n $(ENV) env PYTHONPATH=. SYMBOL=MNQ python -u scripts/slack_alerts.py --summary

alerts-rebuild: ensure-env
	@UPLOAD_CHART=1 SYMBOL=$(SYMBOL) conda run -n $(ENV) env PYTHONPATH=. python -u scripts/slack_alerts.py --rebuild-daily

microlot-status:
	@$(RUN) scripts/microlot_status.py



# -------- SPY / QQQ targets --------

start-spy:
	@mkdir -p logs data
	@nohup $(RUN) scripts/shadow_mes_delayed.py --symbol SPY --exchange SMART --clientId 10 --out data/SPY_live_1m.csv > logs/shadow_spy.out 2>&1 &
	@nohup $(RUN) scripts/run_oos_loop.py --cadence-min 10 --min-bars 150 \
	  --csv data/SPY_live_1m.csv \
	  --config config/overrides/spy_default.yaml \
	  --log logs/oos_log_spy.csv \
	  --trades-csv logs/backtest_trades_spy.csv \
	  --tag SPY \
	  --symbol SPY \
	  > logs/oos_loop_SPY.out 2>&1 &
	@$(RUN) scripts/notify_slack.py "📈 SPY STARTED on $(HOSTNAME)."
	@echo "SPY feeder + OOS loop started"

stop-spy:
	@echo "Stopping SPY feeder + OOS loop..."
	@pkill -f "scripts/shadow_mes_delayed.py --symbol SPY" 2>/dev/null || true
	@pgrep -fl "scripts/run_oos_loop.py --csv data/SPY_live_1m.csv" | awk '{print $$1}' | xargs -r kill
	@$(RUN) scripts/notify_slack.py "🛑 SPY STOPPED on $(HOSTNAME)."
	@echo "SPY feeder + OOS loop stopped"

start-qqq:
	@mkdir -p logs data
	@nohup $(RUN) scripts/shadow_mes_delayed.py --symbol QQQ --exchange SMART --clientId 11 --out data/QQQ_live_1m.csv > logs/shadow_qqq.out 2>&1 &
	@nohup $(RUN) scripts/run_oos_loop.py --cadence-min 10 --min-bars 150 \
	  --csv data/QQQ_live_1m.csv \
	  --config config/overrides/qqq_default.yaml \
	  --log logs/oos_log_qqq.csv \
	  --trades-csv logs/backtest_trades_qqq.csv \
	  --tag QQQ \
	  --symbol QQQ \
	  > logs/oos_loop_QQQ.out 2>&1 &
	@$(RUN) scripts/notify_slack.py "📊 QQQ STARTED on $(HOSTNAME)."
	@echo "QQQ feeder + OOS loop started"

stop-qqq:
	@echo "Stopping QQQ feeder + OOS loop..."
	@pkill -f "scripts/shadow_mes_delayed.py --symbol QQQ" 2>/dev/null || true
	@pgrep -fl "scripts/run_oos_loop.py --csv data/QQQ_live_1m.csv" | awk '{print $$1}' | xargs -r kill
	@$(RUN) scripts/notify_slack.py "🛑 QQQ STOPPED on $(HOSTNAME)."
	@echo "QQQ feeder + OOS loop stopped"

# -------- Setup / Env / Logs --------

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
	@conda run -n $(ENV) env PYTHONPATH=. python -u scripts/slack_alerts.py $(ARGS)

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

# -------- Legacy generic processes / ops --------


restart: stop start

status:
	@echo "== FEEDERS =="
	@ps aux | grep shadow_mes_delayed.py | grep -v grep || true
	@echo "\n== LOOPS =="
	@ps aux | grep run_oos_loop.py | grep -v grep || true
	@echo "\n== MES OOS =="
	@tail -n 5 logs/oos_log.csv 2>/dev/null || true
	@echo "\n== MNQ OOS =="
	@tail -n 5 logs/oos_log_MNQ.csv 2>/dev/null || true

oos-status:
	@pgrep -fl run_oos_loop.py || echo "oos: none"
	@tail -n 20 $(LOGS)/oos_loop.out || true
	@tail -n 3  $(OOS_LOG) || true

health:
	@echo ">> Health (STRICT=1, STALE_SEC=1200)"
	@STRICT=1 STALE_SEC=1200 bash scripts/health_check.sh

	
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
