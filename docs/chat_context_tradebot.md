# Tradebot – Chat Context / Working Notes

Last updated: 2026-01-30

---

## Objective

Establish **deterministic parity** between:
- Manual backtests
- Parameter sweeps

Specifically for:
- ETH/USDT
- Strategy: VwapReversion

We want identical inputs → identical trades → identical summary.json → identical rows in results.csv.

---

## Canonical ETH CSV Path

This is the **only** CSV considered canonical for ETH backtests:

data/ETH_USDT_live_1m.csv

All manual runs and sweeps must point to this file explicitly or via the universe/sweep generator.

---

## Canonical Manual Run Command

Baseline manual run (no sweep):

```bash
PYTHONPATH=. .venv/bin/python run_backtest.py \
  --profile config/profile/crypto.yaml \
  --universe config/universes/crypto_all.yaml \
  --symbol ETH/USDT \
  --csv data/ETH_USDT_live_1m.csv \
  --start-date 2025-12-01 \
  --end-date 2026-01-11
Rules:

--start-date and --end-date must be explicit

Never rely on defaults when comparing against sweeps

Manual runs should not use --outdir unless testing output routing

---

## Sweep Runner Entrypoint

Sweep entrypoint lives in:

scripts/run_sweep.py
(or whatever wrapper generates per-param universe.yaml files)

Each sweep run:

Generates a per-run universe.yaml

Calls run_backtest.py once per parameter combination

Passes:

--universe <generated_universe.yaml>

--outdir <sweep_root>

--run-tag <param_string>

--light-artifacts

Output Directory Rules
Manual Runs

Default:

logs/bt_<SYMBOL>


Example:

logs/bt_ETH_USDT/

Sweep Runs
logs/sweeps/<sweep_id>/<run_tag>/


Example:

logs/sweeps/sweep_ETH_USDT_20260130_124607/target_R0.8_atr_mult1.0/


Each run directory contains:

summary.json

backtest_trades.csv

manifest.json

(optional) equity artifacts

Where summary.json Is Read From

For any run:

<run_dir>/summary.json


For sweeps:

results.csv is constructed by reading each run’s summary.json

Trades and PnL numbers in results.csv must match summary.json exactly

Known Gotchas
1. Date Drift

If manual run and sweep do not share:

identical --start-date

identical --end-date

Results are invalid to compare.

2. Silent Overwrites

Re-running without:

--run-tag

or unique sweep root

can overwrite previous results silently.

3. Strategy Config Leakage

Changing strategy name without replacing the block can leak keys.
Handled by:

merge_strategy_cfg

strategy replacement on name change

4. Crypto DPP Invariant

For crypto:

tick_value / tick_size must == 1.0


Backtest hard-fails if violated.

Required Log Lines (Always Verify)

Every correct run must log:

Strategy resolution

[STRATEGY] ETH/USDT -> VwapReversion


DPP sanity

[DPP] ETH/USDT dollars_per_point=1.0


Exit selection sanity

[EXIT.SEL] mask_true=<n> rows=<m>


Missing any of these means the run is suspect.

Current Hypothesis

Manual and sweep results now match when:

CSV path is identical

Start/end dates are identical

Strategy cfg is confirmed via manifest

Strategy init does not mutate defaults

Observed differences earlier were caused by:

Incorrect sweep start_date

Not config drift or execution bugs

Next Test

Confirm final parity with:

One manual run

One sweep run

Identical parameters

Diff:

summary.json

backtest_trades.csv

manifest.json (sanity only)

If identical:

ETH VwapReversion parity is locked

Proceed to next strategy (RSI Mean Reversion)


---

## Helpers to Maintain This File

### 1. Add a “truth rule”
Any time you discover a rule that would have saved you 30 minutes — add it under **Known Gotchas**.

### 2. Update “Current Hypothesis” aggressively
This is **not documentation**, it’s a working theory.
When proven wrong → overwrite it.

### 3. One change = one line
Don’t rewrite sections unless something fundamental changes.

### 4. Commit it
This file belongs in git. It’s part of your tooling now.

---

## Where We Are Right Now

You are **past the dangerous part**.

- Sweep vs manual mismatch: **resolved**
- Manifest correctness: **verified**
- results.csv aggregation: **correct**
- Strategy init changes: **safe and consistent**

### You do NOT need to re-run another manual test right now.

Next logical moves:
1. Lock this ETH/VwapReversion baseline
2. Copy the same verification pattern to **RSI Mean Reversion**
3. Then move on to parameter research, not debugging

If you want, next we can:
- Add a config fingerprint hash to summary.json
- Or hard-fail sweeps when summary.json != results.csv row
- Or automate parity checks (manual vs sweep diff script)

You’re in a clean state.
