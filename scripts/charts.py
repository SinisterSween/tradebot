# scripts/charts.py
from pathlib import Path
import math
from typing import List, Dict

# Do NOT set backend here; let the caller handle matplotlib backend.
from matplotlib import pyplot as plt

def _parse_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def render_daily_chart(daily_rows: List[Dict], out_png: Path, days: int = 45) -> None:
    """
    Line: Profit Factor (left axis)
    Bars: NetPnL (right axis)
    """
    if not daily_rows:
        return
    series = daily_rows[-days:] if len(daily_rows) > days else daily_rows

    dates = [r["date"] for r in series]
    pf_vals = []
    for r in series:
        v = r.get("profit_factor")
        try:
            fv = float(v)
            if math.isinf(fv):
                pf_vals.append(math.nan)
            else:
                pf_vals.append(fv)
        except Exception:
            pf_vals.append(math.nan)

    pnl_vals = [_parse_float(r.get("net_pnl", 0.0)) for r in series]

    fig = plt.figure(figsize=(10, 4.5), dpi=140)
    ax_pf = plt.gca()
    ax_pf.plot(dates, pf_vals, marker="o", linewidth=1.8)
    ax_pf.set_ylabel("Profit Factor")
    ax_pf.set_xlabel("Date (UTC)")
    if [x for x in pf_vals if not math.isnan(x)]:
        ymin = min(x for x in pf_vals if not math.isnan(x))
        ymax = max(x for x in pf_vals if not math.isnan(x))
        if ymax - ymin < 0.5:
            ax_pf.set_ylim(max(0.0, ymin - 0.25), ymax + 0.25)
    ax_pf.grid(True, linestyle=":", linewidth=0.5, alpha=0.7)

    ax_pnl = ax_pf.twinx()
    ax_pnl.bar(dates, pnl_vals, alpha=0.35)
    ax_pnl.set_ylabel("NetPnL (sum of day)")

    plt.title("Daily Performance (PF line, NetPnL bars)")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)

def render_streak_chart(daily_rows: List[Dict], out_png: Path, days: int = 90) -> None:
    """
    Visualize daily win/loss streaks.
    Positive net_pnl -> green(ish) bar above zero; negative -> red(ish) below zero.
    The chart shows *consecutive* runs visually.
    """
    if not daily_rows:
        return
    series = daily_rows[-days:] if len(daily_rows) > days else daily_rows
    dates = [r["date"] for r in series]
    pnls  = [_parse_float(r.get("net_pnl", 0.0)) for r in series]

    # Simple color map by sign (avoid specifying explicit colors if you prefer; here we let matplotlib pick defaults)
    colors = []
    for v in pnls:
        if v > 0:
            colors.append(None)  # default color
        elif v < 0:
            colors.append(None)  # default; user can set rcParams if desired
        else:
            colors.append(None)

    fig = plt.figure(figsize=(10, 3.8), dpi=140)
    ax = plt.gca()
    ax.bar(dates, pnls)  # no explicit colors to keep things simple
    ax.axhline(0, linewidth=1.0)
    ax.set_ylabel("NetPnL")
    ax.set_title("Streaks (daily NetPnL by sign)")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)

def render_weekly_chart(daily_rows: List[Dict], out_png: Path, weeks: int = 26) -> None:
    """
    Aggregate daily net_pnl into ISO weeks and plot a weekly bar chart.
    Expects daily_rows items with keys: date (YYYY-MM-DD), net_pnl.
    """
    if not daily_rows:
        return
    from datetime import datetime as _dt

    # Build weekly buckets (ISO week: YYYY-Www)
    weekly = {}
    for r in daily_rows:
        try:
            d = _dt.fromisoformat(r["date"]).date()
        except Exception:
            continue
        key = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        weekly.setdefault(key, 0.0)
        weekly[key] += _parse_float(r.get("net_pnl", 0.0))

    labels = sorted(weekly.keys())
    if len(labels) > weeks:
        labels = labels[-weeks:]

    vals = [weekly[k] for k in labels]

    fig = plt.figure(figsize=(10, 3.8), dpi=140)
    ax = plt.gca()
    ax.bar(labels, vals)
    ax.axhline(0, linewidth=1.0)
    ax.set_ylabel("NetPnL")
    ax.set_title("Weekly NetPnL")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)
