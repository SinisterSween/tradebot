# Tradebot — Local Quick Start & Health Checks

> Fast reference for running your MES futures setup (delayed data path) and checking health. Timezone: America/Chicago.

## Setup

```bash
# Create/activate env (if not already)
conda env create -f environment.yml -n tradebot || true
conda activate tradebot

# Install deps / upgrade pip
pip install --upgrade pip
pip install -r requirements.txt
```

## Core Commands

### 1) Start delayed data feeder (IBKR paper → CSV)
Writes live-delayed 1m bars to `data/MES_live_1m.csv`.

```bash
nohup python scripts/shadow_mes_delayed.py > logs/shadow.out 2>&1 &
```

### 2) Start OOS loop backtester (auto-runs when CSV updates)
Appends summarized results to `logs/oos_log.csv`.

```bash
nohup python scripts/run_oos_loop.py   --cadence-min 10   --min-bars 150   --csv data/MES_live_1m.csv   --config config/overrides/clean_v1_be10_tick1.yaml   > logs/oos_loop.out 2>&1 &
```

### 3) One-off backtest
```bash
PYTHONPATH=. python run_backtest.py --config config/overrides/clean_v1_be10_tick1.yaml
```

### 4) Live (paper) dry-run
Uses delayed bars; enforces staleness gate.

```bash
PYTHONPATH=. python run_live.py --config config/settings.dev.yaml --symbol MES --dry-run
```

*(Alt profiles/envs: `--env paper --profile futures` if your code supports those flags.)*

## Health Checks

```bash
# Processes
pgrep -fl shadow_mes_delayed.py
pgrep -fl run_oos_loop.py

# Tails
tail -n 100 logs/shadow.out
tail -n 100 logs/oos_loop.out
tail -n 50  logs/oos_log.csv
tail -n 50  data/MES_live_1m.csv

# Look for feeder heartbeats (should print “[NOOP]” on closed markets)
grep -n "NOOP" logs/shadow.out | tail

# Backtest outputs (after manual runs or OOS loop triggers)
ls -1 logs/ | egrep "pnl_curve|equity_curve|summary.json|backtest_trades.csv" || true
```
#Health Checks SH file
To run the health checks file use this:
scripts/health_check.sh
STRICT=1 STALE_SEC=1200 scripts/health_check.sh
## Expected Files & Dirs

- `data/MES_live_1m.csv` — live-delayed 1m bars from IBKR (feeder output)
- `logs/shadow.out` — feeder log
- `logs/oos_loop.out` — OOS loop log
- `logs/oos_log.csv` — rolling OOS summary (timestamp, Trades, WinRate, PF, Expectancy, NetPnL, etc.)
- `logs/` — backtest artifacts: `pnl_curve.png`, `equity_curve.png`, `summary.json`, `backtest_trades.csv`

## Stop / Clean Up

```bash
# Stop processes
pkill -f shadow_mes_delayed.py || true
pkill -f run_oos_loop.py || true

# Optional: clean generated logs/state
rm -f logs/pnl_curve.png logs/equity_curve.png logs/summary.json logs/backtest_trades.csv
```

## Troubleshooting: IBKR/TWS (Paper)

- If you see delayed data: expected on paper unless live account funded and permissions active.
- Staleness gate for delayed is typically 1200s; you’ll see it logged in `run_live.py`.
- Feeder prints `[NOOP]` during closed market hours; otherwise you should see CSV growing.
- If CSV stalls: check TWS connection, IB gateway, or restart feeder.

## Notes on Strategy & Configs

- Strategy: `HybridOrbVwap` (params come from baseline `profile/env` YAML).
- Best override (current): `config/overrides/clean_v1_be10_tick1.yaml`
  - execution:
    - `be_lock_min: 10`
    - `be_req_profit_ticks: 1`
    - `max_hold_min: 30`
    - `hard_cutoff_R: -0.25`
    - `hard_cutoff_min: 10`
    - `min_minutes_to_window_end: 35`
- OOS loop writes results to `logs/oos_log.csv`; review after sessions to track regime shifts.

## One-Liners (copy/paste)

```bash
# Start both services
nohup python scripts/shadow_mes_delayed.py > logs/shadow.out 2>&1 & && nohup python scripts/run_oos_loop.py --cadence-min 10 --min-bars 150   --csv data/MES_live_1m.csv --config config/overrides/clean_v1_be10_tick1.yaml   > logs/oos_loop.out 2>&1 &

# Health snapshot
pgrep -fl shadow_mes_delayed.py; pgrep -fl run_oos_loop.py; tail -n 10 data/MES_live_1m.csv; tail -n 20 logs/oos_log.csv; tail -n 30 logs/shadow.out | tail -n 30; tail -n 30 logs/oos_loop.out | tail -n 30
```

---

*Keep this file in the repo root as `README_local.md` for fast startup.*
