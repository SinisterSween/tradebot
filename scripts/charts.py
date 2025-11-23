# scripts/charts.py
from pathlib import Path
import math
from datetime import datetime, timezone
import csv
import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt
import pytz
CT = pytz.timezone("America/Chicago")
# --- same core renderers you already had ----------------------------

def _prep_dates(daily, days):
    series = daily[-days:] if len(daily) > days else daily
    labels = [d["date"] for d in series]
    return series, labels

def _dedupe_daily_by_date(daily):
    """
    When OOS runs many times on the same day, we can end up with multiple
    entries for the same YYYY-MM-DD. Keep the *last* one (most recent).
    """
    if not daily:
        return daily
    by_date = {}
    for row in daily:
        d = row.get("date")
        if not d:
            continue
        # overwrite -> last one wins
        by_date[d] = row
    # return in date order
    return [by_date[d] for d in sorted(by_date.keys())]


def render_daily_chart(daily, out_png: Path, days: int = 45, title_prefix: str = ""):
    daily = _dedupe_daily_by_date(daily)
    if not daily:
        return
    series, labels = _prep_dates(daily, days)

    eff_vals = [] 
    pnl_vals = []

    for r in series:
        v = r.get("profit_factor")
        if v is None:
            eff_vals.append(math.nan)
        else:
            try:
                pf = float(v)
                if pf <= 0 or math.isnan(pf):
                    eff_vals.append(0.0)
                else:
                    capped = min(pf, 3.0)
                    eff = (capped / 3.0) * 100.0
                    eff_vals.append(eff)
            except Exception:
                eff_vals.append(math.nan)

        pnl_vals.append(float(r.get("net_pnl", 0.0) or 0.0))

        #<<< Decide if we have meaningful PF data to show>>>
    finite_eff = [x for x in eff_vals if not math.isnan(x)]
    show_eff_line = len(finite_eff) >= 1
    
    fig = plt.figure(figsize=(10, 4), dpi=140)
    ax1 = plt.gca()

    if show_eff_line:
        ax1.plot(labels, eff_vals, marker="o", linewidth=1.5)
    ax1.set_ylabel("Efficiency (%) PF≤3 → 100%")
    ax1.grid(True, linestyle=":", alpha=0.5)
    ax1.set_ylim(0, 105)

    ax2 = ax1.twinx()
    ax2.bar(labels, pnl_vals, alpha=0.35)
    ax2.set_ylabel("NetPnL")

    suffix = f" – {title_prefix}" if title_prefix else ""
    ax1.set_title(f"Daily Performance{suffix}")

    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)

def render_streak_chart(daily, out_png: Path, days: int = 90, title_prefix: str = ""):
    daily = _dedupe_daily_by_date(daily)
    if not daily:
        return
    series, labels = _prep_dates(daily, days)

    signs = []
    for r in series:
        pnl = float(r.get("net_pnl", 0.0) or 0.0)
        if pnl > 0:
            signs.append(1)
        elif pnl < 0:
            signs.append(-1)
        else:
            signs.append(0)

    fig = plt.figure(figsize=(10, 2.8), dpi=140)
    ax = plt.gca()
    colors = []
    for s in signs:
        if s > 0:
            colors.append("#3b82f6")
        elif s < 0:
            colors.append("#ef4444")
        else:
            colors.append("#9ca3af")
    ax.bar(labels, [1]*len(labels), color=colors)
    ax.set_yticks([])
    ax.set_title(f"Streaks{(' – ' + title_prefix) if title_prefix else ''}")
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)

def render_weekly_chart(daily, out_png: Path, weeks: int = 26, title_prefix: str = ""):
    daily = _dedupe_daily_by_date(daily)
    if not daily:
        return
    buckets = []
    cur = []
    for d in reversed(daily):
        cur.append(d)
        if len(cur) == 7:
            buckets.append(cur)
            cur = []
    if cur:
        buckets.append(cur)
    buckets = buckets[:weeks]
    buckets = list(reversed(buckets))

    labels = []
    sums = []
    for wk in buckets:
        labels.append(wk[0]["date"] + "…")
        sums.append(sum(float(x.get("net_pnl", 0.0) or 0.0) for x in wk))

    fig = plt.figure(figsize=(10, 3), dpi=140)
    ax = plt.gca()
    ax.bar(labels, sums)
    ax.set_title(f"Weekly NetPnL{(' – ' + title_prefix) if title_prefix else ''}")
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)

def render_equity_chart(daily, out_png: Path, days: int = 9999, title_prefix: str = ""):
    daily = _dedupe_daily_by_date(daily)
    if not daily:
        return
    series, labels = _prep_dates(daily, days)

    running = 0.0
    eq = []
    for r in series:
        running += float(r.get("net_pnl", 0.0) or 0.0)
        eq.append(running)

    fig = plt.figure(figsize=(10, 3), dpi=140)
    ax = plt.gca()
    ax.plot(labels, eq, linewidth=1.6, marker="o")
    ax.set_title(f"Equity Curve – {title_prefix}" if title_prefix else "Equity Curve")
    ax.set_ylabel("Cum NetPnL")
    ax.grid(True, linestyle=":", alpha=0.5)
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)

def render_real_equity_chart(daily, out_png: Path, title_prefix: str = ""):
    daily = _dedupe_daily_by_date(daily)
    if not daily:
        return

    vals = []
    labels = []
    for r in daily:
        eq = r.get("end_equity")
        if eq is None:
            continue
        vals.append(float(eq))
        labels.append(r["date"])

    if not vals:
        return

    fig = plt.figure(figsize=(10, 3), dpi=140)
    ax = plt.gca()
    ax.plot(labels, vals, marker="o", linewidth=1.5)
    ax.set_title(f"Real Equity – {title_prefix}" if title_prefix else "Real Equity")
    ax.set_ylabel("Account Equity (USD)")
    ax.grid(True, linestyle=":", alpha=0.5)
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)


def render_trades_chart(daily, out_png: Path, days: int = 45, title_prefix: str = ""):
    """
    Simple density view: how many trades we actually got per day.
    Useful for diagnosing why PF is noisy (low N).
    """
    daily = _dedupe_daily_by_date(daily)
    if not daily:
        return
    series, labels = _prep_dates(daily, days)
    vals = [int(r.get("trades", 0) or 0) for r in series]

    fig = plt.figure(figsize=(10, 2.6), dpi=140)
    ax = plt.gca()
    ax.bar(labels, vals)
    ax.set_title(f"Trades / Day – {title_prefix}" if title_prefix else "Trades / Day")
    ax.set_ylabel("# trades")
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)

# --- loader tuned to YOUR CSV shape (12 cols, no symbol) ------------

IDX = {
    "timestamp": 0, "rc": 1, "csv": 2, "config": 3,
    "Trades": 4, "WinRate": 5, "ProfitFactor": 6, "Expectancy": 7,
    "NetPnL": 8, "FeesTotal": 9, "NetReturnPct": 10, "MaxDrawdown": 11,
    "EndEquity": 12,
}

def load_daily_from_oos(oos_path: Path):
    if not oos_path.exists():
        return []

    rows = []
    with oos_path.open("r", encoding="utf-8", errors="ignore") as f:
        rdr = csv.reader(f)
        header = next(rdr, None)
        if header and "EndEquity" in header:
            end_eq_idx = header.index("EndEquity")
        for r in rdr:
            if not r:
                continue
            if len(r) <= IDX["MaxDrawdown"]:
                continue
            rows.append(r)

    if not rows:
        return []

    buckets = {}
    for r in rows:
        ts_str = r[IDX["timestamp"]]
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        day = ts.date().isoformat()
        buckets.setdefault(day, []).append(r)

    daily = []
    for day in sorted(buckets.keys()):
        rs = buckets[day]
        trades = 0
        pnl_sum = 0.0
        gross_win = 0.0
        gross_loss = 0.0
        wins = 0
        losses = 0
        end_equity = None

        for r,  in rs:
            # trades
            try:
                t = float(r[IDX["Trades"]] or 0)
            except Exception:
                t = 0
            trades += int(t)

            # pnl
            try:
                pnl = float(r[IDX["NetPnL"]] or 0)
            except Exception:
                pnl = 0.0
            pnl_sum += pnl
            if pnl > 0:
                wins += 1
                gross_win += pnl
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)
            
            if end_eq_idx is not None and len(r) > end_eq_idx:
                try:
                    end_equity = float(r[end_eq_idx])
                except Exception:
                    pass

        winrate = round(100.0 * wins / max(1, wins + losses), 2)
        if gross_loss == 0 and gross_win > 0:
            pf = float("inf")
        elif gross_loss == 0:
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
            "end_equity": end_equity,
        })
    return daily

# --- orchestrator: read MES + MNQ ----------------------------

def build_all():
    out_dir = Path("logs/charts")
    out_dir.mkdir(parents=True, exist_ok=True)

    symbol_logs = {
        "MES": "logs/oos_log.csv",
        "MNQ": "logs/oos_log_MNQ.csv",
        "SPY": "logs/oos_log_SPY.csv",
        "QQQ": "logs/oos_log_QQQ.csv",
    }

    for sym, log_path in symbol_logs.items():
        p = Path(log_path)
        if not p.exists():
            continue

    # All Charts for any symbol found
        daily = load_daily_from_oos(p)
        if not daily:
            continue

        render_daily_chart(daily, out_dir / f"{sym}_daily.png", title_prefix=sym)
        render_streak_chart(daily, out_dir / f"{sym}_streaks.png", title_prefix=sym)
        render_weekly_chart(daily, out_dir / f"{sym}_weekly.png", title_prefix=sym)
        render_real_equity_chart(daily, out_dir / f"{sym}_equity_real.png", title_prefix=sym)
        render_trades_chart(daily, out_dir / f"{sym}_trades.png", title_prefix=sym)
        render_equity_chart(daily, out_dir / f"{sym}_equity.png", title_prefix=sym)
        
        latest = daily[-1]

        with (out_dir / f"{sym}_latest.txt").open("w") as f:
            f.write(f"symbol={sym}\n")
            for k, v in latest.items():
                f.write(f"{k}={v}\n")
    

def main():
    build_all()

if __name__ == "__main__":
    main()
