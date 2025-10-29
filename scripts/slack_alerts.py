#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, csv, time, subprocess, json, hashlib
from pathlib import Path
from datetime import datetime, timezone
import pytz
import socket
import math
import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt
from scripts.charts import render_daily_chart, render_streak_chart, render_weekly_chart

import fcntl
def _acquire_lock():
    Path("logs").mkdir(exist_ok=True)
    f = open("logs/.alerts.lock", "w")
    try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: 
        print("[SKIP] another alerts run is active"); sys.exit(0)
    return f  # keep handle open

# --- Paths ---
DATA_CSV   = Path("data/MES_live_1m.csv")
OOS_LOG    = Path("logs/oos_log.csv")
TRADES_CSV = Path("logs/backtest_trades.csv")
FEED_LOG   = Path("logs/shadow.out")
NOTIFY     = Path("scripts/notify_slack.py")

# --- Config (kept from your file, with sane defaults for delayed data ops) ---
STALE_SEC = int(os.getenv("STALE_SEC", "1200"))   # 20m default (delayed data gate)
PF_WARN   = float(os.getenv("PF_WARN", "1.2"))
EXP_WARN  = float(os.getenv("EXP_WARN", "10.0"))
HIST_RUNS = int(os.getenv("HIST_RUNS", "3"))
PF_DROP_WINDOW = int(os.getenv("PF_DROP_WINDOW", "10"))
PF_DROP_PCT    = float(os.getenv("PF_DROP_PCT", "0.40"))  # 40%

CT = pytz.timezone("America/Chicago")
STATE = Path("logs/.alerts_state.json")

# --- Column indexes for your no-header oos_log.csv ---
IDX = {
    "timestamp": 0, "rc": 1, "csv": 2, "config": 3,
    "Trades": 4, "WinRate": 5, "ProfitFactor": 6, "Expectancy": 7,
    "NetPnL": 8, "FeesTotal": 9, "NetReturnPct": 10, "MaxDrawdown": 11,
}

# ---------- Helpers ----------
def _changed(key: str, payload: str) -> bool:
    """Return True if payload is new for this key; persist hash to suppress dupes."""
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    h = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()
    if state.get(key) == h:
        return False
    state[key] = h
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")
    return True

def run_notify(msg: str):
    """Send Slack message (pass message as CLI arg to match your notify_slack.py)."""
    try:
        subprocess.run([sys.executable, str(NOTIFY), msg], check=False)
    except Exception as e:
        print(f"[WARN] notify_slack failed: {e}")

def is_rth_now() -> bool:
    """
    CME equities (ES/MES) simplified session guard:
    Sun 5:00pm CT → Fri 4:00pm CT, daily maintenance ~4–5pm.
    Gates stale-feed alerts so weekends/maintenance don't spam.
    """
    now = datetime.now(CT)
    if now.hour == 16:  # maintenance-ish
        return False
    wd = now.weekday()  # Mon=0 .. Sun=6
    if wd == 6:  # Sunday
        return now.hour >= 17
    if 0 <= wd <= 3:  # Mon–Thu
        return True
    if wd == 4:  # Friday
        return now.hour < 16
    return False

def _read_last_line(path: Path) -> str | None:
    if not path.exists():
        return None
    last = None
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.strip():
                last = line.strip()
    return last

def _tail_rows(path: Path, n: int) -> list[list[str]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            rows.append(ln.split(","))
    return rows[-n:] if n > 0 else rows

def _to_float(x) -> float | None:
    try:
        return float(x)
    except Exception:
        return None

def load_last_bar_age():
    if not DATA_CSV.exists():
        return None
    try:
        age = max(0, time.time() - DATA_CSV.stat().st_mtime)
        ts = None
        with DATA_CSV.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                pass
            if line:
                ts = line.split(",")[0].strip()
        return {"age": age, "ts": ts}
    except Exception:
        return None

def load_trades(limit: int = 500):
    """Return list[dict] from backtest_trades.csv (or [] if missing)."""
    if not TRADES_CSV.exists():
        return []
    rows = []
    with TRADES_CSV.open("r", newline="", encoding="utf-8", errors="ignore") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    return rows[-limit:]

# ---------- Checks ----------
def check_feed_stale():
    # Only alert on staleness during RTH
    if not is_rth_now():
        return None
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
        last = _read_last_line(OOS_LOG)
        if not last:
            return "⚠️ OOS log empty."
        parts = last.split(",")
        rc = 0
        if len(parts) > IDX["rc"]:
            try:
                rc = int(float(parts[IDX['rc']]))
            except Exception:
                rc = 0
        if rc != 0:
            return f"🛑 OOS loop crashed (rc={rc}). Check logs/oos_loop.out"
    except Exception:
        return "⚠️ OOS parse error."
    return None

def check_performance_warning():
    if not OOS_LOG.exists():
        return None
    try:
        rows = _tail_rows(OOS_LOG, max(HIST_RUNS, PF_DROP_WINDOW))
        if not rows:
            return None

        # A) relative PF drop over PF_DROP_WINDOW
        pf_window = []
        for r in rows[-PF_DROP_WINDOW:]:
            if len(r) <= IDX["ProfitFactor"]:
                continue
            v = _to_float(r[IDX["ProfitFactor"]])
            if v is not None:
                pf_window.append(v)
        if len(pf_window) >= 5:
            peak = max(pf_window)
            curr = pf_window[-1]
            if peak > 0 and (peak - curr) / peak >= PF_DROP_PCT:
                return f"🔻 PF drop: {curr:.2f} from peak {peak:.2f} (>{int(PF_DROP_PCT*100)}% decline)"

        # B) avg PF / Expect over last HIST_RUNS
        tail = rows[-HIST_RUNS:]
        pf_vals, exp_vals = [], []
        for r in tail:
            if len(r) > IDX["ProfitFactor"]:
                v = _to_float(r[IDX["ProfitFactor"]])
                if v is not None:
                    pf_vals.append(v)
            if len(r) > IDX["Expectancy"]:
                v = _to_float(r[IDX["Expectancy"]])
                if v is not None:
                    exp_vals.append(v)

        if pf_vals and exp_vals:
            pf_avg  = sum(pf_vals) / len(pf_vals)
            exp_avg = sum(exp_vals) / len(exp_vals)
            if pf_avg < PF_WARN or exp_avg < EXP_WARN:
                return f"⚠️ Performance dip: PF={pf_avg:.2f}, Expect=${exp_avg:.2f} (avg of {len(pf_vals)} runs)"

        # C) consecutive losing EXITs from backtest_trades.csv (optional)
        try:
            trows = load_trades(limit=500)
            exits = [r for r in trows if (r.get("action") or "").upper() == "EXIT"]
            pnl_key = next((k for k in ("pnl","PnL","pnl_net","net_pnl","profit")
                            if exits and k in exits[0].keys()), None)
            if pnl_key:
                streak = 0
                for r in exits:
                    v = _to_float(r.get(pnl_key))
                    v = v if v is not None else 0.0
                    if v <= 0:
                        streak += 1
                    else:
                        streak = 0
                if streak >= 3:
                    return f"⚠️ Loss streak: {streak} consecutive losing exits"
        except Exception:
            pass

    except Exception:
        return None
    return None

def daily_summary():
    """One-line summary using the *latest* OOS row."""
    if not OOS_LOG.exists():
        return None
    try:
        last = _read_last_line(OOS_LOG)
        if not last:
            return None
        parts = last.split(",")
        if len(parts) <= IDX["MaxDrawdown"]:
            return None
        trades = parts[IDX["Trades"]]
        win    = parts[IDX["WinRate"]]
        pf     = parts[IDX["ProfitFactor"]]
        exp    = parts[IDX["Expectancy"]]
        pnl    = parts[IDX["NetPnL"]]
        now_ct = datetime.now(CT).strftime('%a %m/%d %I:%M %p CT')
        return f"📊 Daily Summary — {now_ct}\nTrades={trades} | Win%={win} | PF={pf} | Expect=${exp} | NetPnL=${pnl}"
    except Exception:
        return None

# ---------- Daily analytics ----------
def rebuild_daily_perf_and_streaks():
    """
    Build logs/daily_perf.csv (UTC date-bucket) and logs/streaks.txt (win/loss streaks by daily NetPnL sign).
    Returns (daily_rows, streaks_dict)
    """
    if not OOS_LOG.exists():
        return [], {"current": 0, "current_type": "none", "best_win": 0, "best_loss": 0}

    # Load all rows
    rows = _tail_rows(OOS_LOG, 0)
    if not rows:
        return [], {"current": 0, "current_type": "none", "best_win": 0, "best_loss": 0}

    # Group by UTC date from timestamp col
    buckets = {}
    for r in rows:
        if len(r) <= IDX["MaxDrawdown"]:
            continue
        ts_str = r[IDX["timestamp"]]
        try:
            # e.g., 2025-10-28T20:54:52
            ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        except Exception:
            # best-effort: treat as naive UTC
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                else:
                    ts = ts.astimezone(timezone.utc)
            except Exception:
                continue
        day = ts.date().isoformat()

        buckets.setdefault(day, []).append(r)

    daily = []
    for day in sorted(buckets.keys()):
        rs = buckets[day]
        trades = 0
        wins = 0
        losses = 0
        pnl_sum = 0.0
        gross_win = 0.0
        gross_loss = 0.0
        for r in rs:
            t = _to_float(r[IDX["Trades"]]) or 0
            trades += int(t)
            pnl = _to_float(r[IDX["NetPnL"]]) or 0.0
            pnl_sum += pnl
            if pnl > 0:
                wins += 1
                gross_win += pnl
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)
        winrate = round(100.0 * wins / max(1, wins + losses), 2)
        if gross_loss == 0 and gross_win > 0:
            pf = float('inf')
        elif gross_loss == 0 and gross_win == 0:
            pf = 0.0
        else:
            pf = round(gross_win / gross_loss, 4)
        daily.append({
            "date": day,
            "runs": len(rs),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "winrate_pct": winrate,
            "profit_factor": pf,
            "net_pnl": round(pnl_sum, 2),
        })

    # Write logs/daily_perf.csv
    out_csv = Path("logs/daily_perf.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["date","runs","trades","wins","losses","winrate_pct","profit_factor","net_pnl"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in daily:
            w.writerow(r)

    # Compute streaks from daily net_pnl sign
    def sign(x: float) -> int:
        return 1 if x > 0 else (-1 if x < 0 else 0)

    current = 0
    current_type = 0
    best_win = 0
    best_loss = 0
    for d in sorted(daily, key=lambda x: x["date"]):
        s = sign(float(d["net_pnl"]))
        if s == 0:
            current = 0
            current_type = 0
            continue
        if current == 0:
            current = 1
            current_type = s
        elif s == current_type:
            current += 1
        else:
            if current_type == 1:
                best_win = max(best_win, current)
            elif current_type == -1:
                best_loss = max(best_loss, current)
            current = 1
            current_type = s
    # flush tail
    if current_type == 1:
        best_win = max(best_win, current)
    elif current_type == -1:
        best_loss = max(best_loss, current)

    streaks = {"current": current, "current_type": "win" if current_type == 1 else ("loss" if current_type == -1 else "none"),
               "best_win": best_win, "best_loss": best_loss}

    # Write logs/streaks.txt
    out_txt = Path("logs/streaks.txt")
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text(
        f"Current streak: {streaks['current']} ({streaks['current_type']})\n"
        f"Best win streak: {streaks['best_win']}\n"
        f"Best loss streak: {streaks['best_loss']}\n"
        f"Last date: {daily[-1]['date'] if daily else 'n/a'}\n",
        encoding="utf-8",
    )
    return daily, streaks

def _render_and_maybe_upload_charts(daily):
    # --- Render all charts ---
    render_daily_chart(daily, Path("logs/daily_perf.png"), days=45)
    render_streak_chart(daily, Path("logs/streaks_history.png"), days=90)

    # Friday-only weekly rollup
    weekly_now = datetime.now(CT).weekday() == 4
    if weekly_now:
        render_weekly_chart(daily, Path("logs/weekly_perf.png"), weeks=26)

    # --- Optional upload ---
    if os.environ.get("UPLOAD_CHART") == "1":
        to_send = [
            ("logs/daily_perf.png",      "Daily Performance"),
            ("logs/streaks_history.png", "Streaks History"),
        ]
        if weekly_now:
            to_send.append(("logs/weekly_perf.png", "Weekly NetPnL"))

        for img, title in to_send:
            p = Path(img)
            if not p.exists():
                continue
            try:
                size = p.stat().st_size
                if size < 5_000:  # skip small/corrupt renders
                    print(f"[upload skip] {img} too small ({size} bytes)")
                    continue
            except Exception as e:
                print(f"[upload error] stat({img}) failed: {e}")
                continue

            comment = f"{title} ({socket.gethostname()})"
            rc = subprocess.run(
                [sys.executable, "scripts/notify_slack_file.py",
                 str(p), title, comment],
                capture_output=True, text=True
            )
            print(f"[upload rc={rc.returncode}] {rc.stdout or rc.stderr}".strip())

    return weekly_now

# ---------- Main ----------
def main():
    _lock = _acquire_lock()
    # CLI flags
    do_summary  = "--summary" in sys.argv
    do_rebuild  = "--rebuild-daily" in sys.argv  # analytics-only mode

    daily, streaks = rebuild_daily_perf_and_streaks()

    if do_rebuild:
        #chart + optional upload, then exit
        weekly_now = _render_and_maybe_upload_charts(daily)
        if weekly_now:
            alerts.append("🗓️ Weekly roll-up posted with charts.")
        print(f"[OK] Rebuilt daily_perf.csv (rows={len(daily)}) and streaks.txt ({streaks})")
        return

        

    # -------- Normal alerts path --------
    alerts = []
    for func in (check_feed_stale, check_oos_failure, check_performance_warning):
        msg = func()
        if msg:
            alerts.append(msg)

    if do_summary:
        summary = daily_summary()
        if summary:
            alerts.append(summary)

    # Always rebuild daily metrics + chart on every run
    weekly_now = _render_and_maybe_upload_charts(daily)
    if weekly_now:
        alerts.append("📈 Weekly roll-up posted with charts.")


    # Append streaks line
    alerts.append(
        f"Streaks — current={streaks['current']} ({streaks['current_type']}), "
        f"best_win={streaks['best_win']}, best_loss={streaks['best_loss']}"
    )

    print(f"[OK] alerts check @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    if not alerts:
        print("[OK] No alerts triggered.")
        return

    final_msg = "\n".join(alerts)
    print(final_msg)
    final_msg = f"[*{socket.gethostname()}*]\n" + final_msg
    final_msg += "\n(Chart saved: logs/daily_perf.png)"

    key = f"alerts_text::{socket.gethostname()}"
    if _changed(key, final_msg):
        run_notify(final_msg)
    else:
        print("[OK] duplicate alert suppressed")


if __name__ == "__main__":
    main()
