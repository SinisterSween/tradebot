#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, csv, time, subprocess, json, hashlib, math, socket
from pathlib import Path
from datetime import datetime, timezone
import pytz
import fcntl
import matplotlib
matplotlib.use("Agg")
from scripts.charts import (
    render_daily_chart,
    render_streak_chart,
    render_weekly_chart,
    render_equity_chart,
    render_trades_chart
)

# ---------------------------------------------------------------------
# locking so launchd / cron doesn't run two at once
# ---------------------------------------------------------------------
def _acquire_lock():
    Path("logs").mkdir(exist_ok=True)
    f = open("logs/.alerts.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[SKIP] another alerts run is active")
        sys.exit(0)
    return f  # keep handle open

# ---------------------------------------------------------------------
# config / globals
# ---------------------------------------------------------------------
CT = pytz.timezone("America/Chicago")

# single-symbol env override (still useful)
ENV_SYMBOL = os.getenv("SYMBOL", "MES").upper()

# Multi-symbol map – THIS is what we walk over
# MES keeps the old filenames, others get suffixed
SYMBOLS = [
    ("MES", Path("logs/oos_log.csv")),
    ("MNQ", Path("logs/oos_log_MNQ.csv")),
    # ("M2K", Path("logs/oos_log_M2K.csv")),
]

# these are “global” MES defaults – we’ll rebind per-symbol
DATA_CSV = Path("data/MES_live_1m.csv")
FEED_LOG = Path("logs/shadow.out")
NOTIFY   = Path("scripts/notify_slack.py")
STATE    = Path("logs/.alerts_state.json")

STALE_SEC      = int(os.getenv("STALE_SEC", "1200"))
PF_WARN        = float(os.getenv("PF_WARN", "1.2"))
EXP_WARN       = float(os.getenv("EXP_WARN", "10.0"))
HIST_RUNS      = int(os.getenv("HIST_RUNS", "3"))
PF_DROP_WINDOW = int(os.getenv("PF_DROP_WINDOW", "10"))
PF_DROP_PCT    = float(os.getenv("PF_DROP_PCT", "0.40"))

# no-header oos_log col order
IDX = {
    "timestamp": 0, "rc": 1, "csv": 2, "config": 3,
    "Trades": 4, "WinRate": 5, "ProfitFactor": 6, "Expectancy": 7,
    "NetPnL": 8, "FeesTotal": 9, "NetReturnPct": 10, "MaxDrawdown": 11,
}

# ---------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------
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
    try:
        subprocess.run([sys.executable, str(NOTIFY), msg], check=False)
    except Exception as e:
        print(f"[WARN] notify_slack failed: {e}")

def is_rth_now() -> bool:
    now = datetime.now(CT)
    if now.hour == 16:
        return False
    wd = now.weekday()
    if wd == 6:  # Sun
        return now.hour >= 17
    if 0 <= wd <= 3:
        return True
    if wd == 4:
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

def _to_float(x):
    try:
        return float(x)
    except Exception:
        return None

# ---------------------------------------------------------------------
# per-symbol loaders
# ---------------------------------------------------------------------
def load_last_bar_age(csv_path: Path):
    if not csv_path.exists():
        return None
    try:
        age = max(0, time.time() - csv_path.stat().st_mtime)
        ts = None
        with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                pass
            if line:
                ts = line.split(",")[0].strip()
        return {"age": age, "ts": ts}
    except Exception:
        return None

def load_trades_csv(symbol: str) -> Path:
    # MES keeps old name to avoid breaking old dashboards
    if symbol == "MES":
        return Path("logs/backtest_trades.csv")
    return Path(f"logs/backtest_trades_{symbol}.csv")

def load_trades(symbol: str, limit: int = 500):
    p = load_trades_csv(symbol)
    if not p.exists():
        return []
    rows = []
    with p.open("r", newline="", encoding="utf-8", errors="ignore") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    return rows[-limit:]

def load_daily_from_oos(oos_path: Path) -> list[dict]:
    """
    Convert <logs/oos_log_*.csv> into the same 'daily' structure rebuild_daily_perf_and_streaks() returns.
    """
    rows = _tail_rows(oos_path, 0)
    if not rows:
        return []
    buckets = {}
    for r in rows:
        if len(r) <= IDX["MaxDrawdown"]:
            continue
        ts_str = r[IDX["timestamp"]]
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
        trades = wins = losses = 0
        pnl_sum = gross_win = gross_loss = 0.0
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
            pf = float("inf")
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
    return daily

# ---------------------------------------------------------------------
# per-symbol checks (take symbol + paths!)
# ---------------------------------------------------------------------
def check_feed_stale_for(symbol: str) -> str | None:
    csv_path = Path(f"data/{symbol}_live_1m.csv") if symbol != "MES" else DATA_CSV
    if not is_rth_now():
        return None
    bar = load_last_bar_age(csv_path)
    if not bar:
        return f"⚠️ {symbol}: Feed missing — no CSV found."
    if bar["age"] > STALE_SEC:
        mins = int(bar["age"] // 60)
        return f"⚠️ {symbol}: Feed stale — no new bars for {mins}m (last bar {bar['ts']})."
    return None

def check_oos_failure_for(symbol: str, oos_path: Path) -> str | None:
    if not oos_path.exists():
        return f"⚠️ {symbol}: OOS log missing."
    try:
        last = _read_last_line(oos_path)
        if not last:
            return f"⚠️ {symbol}: OOS log empty."
        parts = last.split(",")
        rc = 0
        if len(parts) > IDX["rc"]:
            try:
                rc = int(float(parts[IDX["rc"]]))
            except Exception:
                rc = 0
        if rc != 0:
            return f"🛑 {symbol}: OOS loop crashed (rc={rc}). Check logs/oos_loop.out"
    except Exception:
        return f"⚠️ {symbol}: OOS parse error."
    return None

def check_performance_warning_for(symbol: str, oos_path: Path) -> str | None:
    if not oos_path.exists():
        return None
    rows = _tail_rows(oos_path, max(HIST_RUNS, PF_DROP_WINDOW))
    if not rows:
        return None

    # A) PF drop
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
            return f"🔻 {symbol}: PF drop {curr:.2f} from peak {peak:.2f} (>{int(PF_DROP_PCT*100)}% decline)"

    # B) avg PF/Expect
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
            return f"⚠️ {symbol}: Performance dip: PF={pf_avg:.2f}, Expect=${exp_avg:.2f} (avg {len(pf_vals)})"

    # C) loss streak from trades CSV
    try:
        trows = load_trades(symbol, limit=500)
        exits = [r for r in trows if (r.get("action") or "").upper() == "EXIT"]
        if exits:
            pnl_key = next(
                (k for k in ("pnl","PnL","pnl_net","net_pnl","profit") if k in exits[0]),
                None
            )
        else:
            pnl_key = None
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
                return f"⚠️ {symbol}: Loss streak {streak} consecutive losing exits"
    except Exception:
        pass

    return None

def daily_summary_for(symbol: str, oos_path: Path) -> str | None:
    if not oos_path.exists():
        return None
    last = _read_last_line(oos_path)
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
    return (
        f"📊 {symbol} Daily Summary — {now_ct}\n"
        f"Trades={trades} | Win%={win} | PF={pf} | Expect=${exp} | NetPnL=${pnl}"
    )

# ---------------------------------------------------------------------
# charts per symbol
# ---------------------------------------------------------------------
def _render_and_maybe_upload_charts_for(symbol: str, daily: list[dict]) -> bool:
    base = Path("logs/charts")
    base.mkdir(parents=True, exist_ok=True)

    daily_png   = base / f"{symbol}_daily.png"
    streaks_png = base / f"{symbol}_streaks.png"
    weekly_png  = base / f"{symbol}_weekly.png"
    equity_png = base / f"{symbol}_equity.png"
    trades_png = base / f"{symbol}_trades.png"

    render_daily_chart(daily, daily_png, days=45, title_prefix=symbol)
    render_streak_chart(daily, streaks_png, days=90, title_prefix=symbol)
    render_equity_chart(daily, equity_png, title_prefix=symbol)
    render_trades_chart(daily, trades_png, days=45, title_prefix=symbol)

    weekly_now = False
    if datetime.now(CT).weekday() == 4:
        render_weekly_chart(daily, weekly_png, weeks=26, title_prefix=symbol)
        weekly_now = True

    if os.environ.get("UPLOAD_CHART") == "1":
        to_send = [
            (daily_png,   f"{symbol} Daily Performance"),
            (streaks_png, f"{symbol} Streaks History"),
            (equity_png,  f"{symbol} Equity Curve"),
            (trades_png,  f"{symbol} Trades History"),
        ]
        if weekly_now:
            to_send.append((weekly_png, f"{symbol} Weekly NetPnL"))

        for png_path, title in to_send:
            if png_path.exists():
                comment = f"{title} ({socket.gethostname()})"
                try:
                    rc = subprocess.run(
                        [sys.executable, "scripts/notify_slack_file.py",
                        str(png_path), title, comment],
                        capture_output=True, text=True
                    )
                    if rc.returncode != 0:
                        print(f"[upload {symbol} rc={rc.returncode}] {rc.stdout or rc.stderr}".strip())
                except Exception as e:
                    print(f"[upload {symbol} ERROR] {e}")
                print(f"[upload {symbol} rc={rc.returncode}] {rc.stdout or rc.stderr}".strip())
    return weekly_now

# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------
def main():
    _lock = _acquire_lock()
    do_summary  = "--summary" in sys.argv
    do_rebuild  = "--rebuild-daily" in sys.argv

    alerts = []
    weekly_any = False

    # If user said SYMBOL=MNQ make alerts, allow single-symbol mode
    if ENV_SYMBOL != "ALL":
        active_symbols = [(s, p) for (s, p) in SYMBOLS if s == ENV_SYMBOL]
        # if they asked for MNQ but we don't have its log yet, fall back to MES
        if not active_symbols:
            active_symbols = [SYMBOLS[0]]
    else:
        active_symbols = SYMBOLS

    if do_rebuild:
        for sym, oos_path in active_symbols:
            daily = load_daily_from_oos(oos_path)
            weekly_now = _render_and_maybe_upload_charts_for(sym, daily)
            if weekly_now:
                alerts.append(f"🗓️ {sym}: Weekly roll-up posted.")
        print(f"[OK] Rebuilt for {[s for s,_ in active_symbols]}")
        # if they only wanted rebuild, don't spam slack
        return

    # normal alerts path
    for sym, oos_path in active_symbols:
        # per-symbol checks
        m = check_feed_stale_for(sym)
        if m: alerts.append(m)

        m = check_oos_failure_for(sym, oos_path)
        if m: alerts.append(m)

        m = check_performance_warning_for(sym, oos_path)
        if m: alerts.append(m)

        if do_summary:
            m = daily_summary_for(sym, oos_path)
            if m: alerts.append(m)

        # charts + maybe upload
        daily = load_daily_from_oos(oos_path)
        if daily:
            if _render_and_maybe_upload_charts_for(sym, daily):
                weekly_any = True

    if weekly_any:
        alerts.append("📈 Weekly roll-up posted with charts.")

    # print + send
    print(f"[OK] alerts check @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    if not alerts:
        print("[OK] No alerts triggered.")
        return

    final_msg = "\n".join(alerts)
    final_msg = f"[*{socket.gethostname()}*]\n" + final_msg

    key = f"alerts_text::{socket.gethostname()}"
    if _changed(key, final_msg):
        run_notify(final_msg)
    else:
        print("[OK] duplicate alert suppressed")


if __name__ == "__main__":
    main()
