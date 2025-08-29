SHELL := /bin/bash

ENV  := tradebot
CSV  := data/ES_1m.csv
LOGS := logs
EXEC_LOG := $(LOGS)/executions.csv

.PHONY: help setup backtest live data fetch-data metrics-up metrics-down tail clean report report-fast sweep ensure-env


help:
	@echo "make setup        # install deps into '$(ENV)'" ; \
	@echo "make backtest     # run backtest on $(CSV)" ; \
	@echo "make live         # run live (paper) bot via IBKR" ; \
	@echo "make metrics-up   # start Prometheus + Grafana" ; \
	@echo "make metrics-down # stop Prometheus + Grafana" ; \
	@echo "make tail         # tail live execution log" ; \
	@echo "make clean        # remove generated logs/state" ; \
	@echo "make report	 # run backtest (no GUI) then open outputs" ; \
	@echo "make report-fast	 # open last backtest outputs if present" ; \
	@echo "make sweep 	 # run parameter sweep and open results" ; \
	

setup: ensure-env
	conda run -n $(ENV) pip install --upgrade pip ; \
	conda run -n $(ENV) pip install -r requirements.txt

backtest: ensure-env 
	@test -f $(CSV) || (echo "Missing $(CSV). Put ES 1m data there." && exit 1) ; \
	@mkdir -p $(LOGS)
	conda run -n $(ENV) python run_backtest.py

live: ensure-env
	@mkdir -p $(LOGS)
	conda run -n $(ENV) python run_live.py

data: fetch-data 
fetch-data: ensure-env
	conda run -n $(ENV) python scripts/get_es_ib.py

metrics-up: 
	@if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then \
		docker compose up -d
	else \
		echo "docker compse not available on this machine. Skipping."; \
	fi
metrics-down: 
	@if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then \
		docker compose down; \
	else \
		echo "docker compose not available. Nothing to stop."; \
	fi

tail: 
	@mkdir -p $(LOGS)
	@touch $(EXEC_LOG)
	tail -n 50 -f $(EXEC_LOG)
y

report: ensure-env
	@mkdir -p $(LOGS)
	conda run -n $(ENV) python run_backtest.py --no-gui
        @test -f $(LOGS)/backtest_trades.csv && open $(LOGS)/backtest_trades.csv || true
        @test -f $(LOGS)/equity_curve.png && open $(LOGS)/equity_curve.png || true

report-fast:
	@test -f $(LOGS)/backtest_trades.csv && open $(LOGS)/backtest_trades.csv || echo "No trades CSV yet."
	@test -f $(LOGS)/equity_curve.png && open $(LOGS)/equity_curve.png || echo "No equity curve yet."

sweep: ensure-env
	@mkdir -p $(LOGS)
	conda run -n $(ENV) env PYTHONPATH=. python scripts/sweep.py
        test -f $(LOGS)/sweep_results.csv && open $(LOGS)/sweep_results.csv || true

clean:
	rm -f logs/*.csv state/*.json 
	@echo "Cleaned logs/ and state/"

ensure-env:
	@conda run -n $(ENV) python -c "import sys; sys.exit(0)" >/dev/null 2>&1 || \
	( echo "Conda env '$(ENV)' not found. Create it first: conda create -n $(ENV) python=3.10 -y" && exit 1 )

