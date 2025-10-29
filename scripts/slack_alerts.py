#!/usr/bin/env python3
import os, sys, csv, time, subprocess, json
from pathlib import Path
from datetime import datetime, timedelta, timezone

# --- Paths ---
DATA_CSV = Path("data/MES_live_1m.csv")
OOS_LOG = Path("logs/oos_log.csv")
FEED_LOG = Path("logs/shadow.out")
NOTIFY = Path("scripts/notify_slack.py")

# --- Config ---
STALE_SEC = 300       # alert if CSV older than 5m
PF_WARN = 1.2         # warn if PF < 1.2 for last few runs
EXP_WARN = 10.0       # warn if Expectancy < $10 for last few runs
HIST_RUNS = 3         # how many recent OOS runs to average
TIMEZONE = timezone(timedelta(hours=-5))  # Central Time (CST/CDT)

def run_notify(msg):
    try:
        subprocess.run(["python", str(NOTIFY), msg], check=False)
    except Exception as e:
        print(f"[WARN] notify_slack failed: {e}")

def load_last_bar_age():
    if not DATA_CSV.exists():
        return None
    try:
        age = max(0, time.time() - DATA_CSV.stat().st_mtime)
        ts = None
        with open(DATA_CSV, "r") as f:
            lines = f.readlines()
            if lines:
                ts = lines[-1].split(",")[0].strip()
        return {"age": age, "ts": ts}
    except Exception:
        return None

def check_feed_stale():
    bar = load_last_bar_age()
    if not bar:
        return "⚠️ Feed missing — no CSV found."
    if bar["age"] > STALE_SEC:
        mins = int(bar["age"] // 60)
        return f"⚠️ Feed stale — no new bars for {mins}m (last bar {bar['ts']})."
    return None

def check_oos_failure():
    if not OOS_LOG.exists():
        return "⚠️ OOS log missing."
    try:
        with open(OOS_LOG, "r") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if not lines:
            return "⚠️ OOS log empty."
        last = lines[-1].split(",")
        if len(last) >= 2 and last[1].isdigit() and int(last[1]) != 0:
            return f"🛑 OOS loop crashed (rc={last[1]}). Check logs/oos_loop.out"
    except Exception:
        return "⚠️ OOS parse error."
    return None

def check_performance_warning():
    if not OOS_LOG.exists():
        return None
    try:
        with open(OOS_LOG, "r") as f:
            rows = [r.strip().split(",") for r in f if r.strip()]
        tail = rows[-HIST_RUNS:]
        if not tail:
            return None

        pf_vals, exp_vals = [], []
        for r in tail:
            try:
                pf_vals.append(float(r[6]))
                exp_vals.append(float(r[7]))
            except:
                pass

        if not pf_vals or not exp_vals:
            return None

        pf_avg = sum(pf_vals) / len(pf_vals)
        exp_avg = sum(exp_vals) / len(exp_vals)
        if pf_avg < PF_WARN or exp_avg < EXP_WARN:
            return f"🔻 Performance dip: PF={pf_avg:.2f}, Expect=${exp_avg:.2f} (avg of {len(pf_vals)} runs)"
    except Exception:
        return None
    return None

def daily_summary():
    if not OOS_LOG.exists():
        return None
    try:
        with open(OOS_LOG, "r") as f:
            rows = [r.strip().split(",") for r in f if r.strip()]
        if not rows:
            return None
        last = rows[-1]
        if len(last) < 12:
            return None
        trades = last[4]
        win = last[5]
        pf = last[6]
        exp = last[7]
        pnl = last[8]
        msg = (
            f"📊 Daily Summary — {datetime.now(TIMEZONE).strftime('%a %m/%d %I:%M %p CT')}\n"
            f"Trades={trades} | Win%={win} | PF={pf} | Expect=${exp} | NetPnL=${pnl}"
        )
        return msg
    except Exception:
        return None

def main():
    alerts = []

    # Core checks
    for func in [check_feed_stale, check_oos_failure, check_performance_warning]:
        msg = func()
        if msg:
            alerts.append(msg)

    # Optional daily summary if run with --summary
    if "--summary" in sys.argv:
        summary = daily_summary()
        if summary:
            alerts.append(summary)
    
    # Always log a heartbeat line
    print(f"[OK] alerts check @ {datetime.now().isoformat(timespec='seconds')}")

    if not alerts:
        print("[OK] No alerts triggered.")
        return

    # Send combined Slack message
    final_msg = "\n".join(alerts)
    print(final_msg)
    run_notify(final_msg)

if __name__ == "__main__":
    main()
