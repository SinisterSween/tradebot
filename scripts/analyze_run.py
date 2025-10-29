#!/usr/bin/env python3
"""
Reads:
  - logs/oos_log.csv            (your OOS summaries, one line per run)
  - logs/backtest_trades.csv    (entries/exits with pnl if present)
  - data/MES_live_1m.csv        (to detect gaps / stall windows)

Emits to logs/:
  - live_summary.txt            (plain-English rollup)
  - oos_recent.csv              (last 50 OOS rows)
  - trades_recent.csv           (last 200 trades/rows)
  - gaps.csv                    (time gaps > 3 minutes in bar stream)
"""
from __future__ import annotations
import csv, math, os, sys, time, statistics as stats
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT/"logs"
DATA = ROOT/"data"
OOS  = LOGS/"oos_log.csv"
TRD  = LOGS/"backtest_trades.csv"
CSV  = DATA/"MES_live_1m.csv"
OUT_SUMMARY = LOGS/"live_summary.txt"
OUT_OOS     = LOGS/"oos_recent.csv"
OUT_TRD     = LOGS/"trades_recent.csv"
OUT_GAPS    = LOGS/"gaps.csv"

def read_csv_rows(p: Path):
    if not p.exists(): return []
    with p.open("r", newline="") as f:
        return [r for r in csv.reader(f) if r]

def as_float(x, default=None):
    try: return float(x)
    except: return default

def tail(lst, n): return lst[-n:] if len(lst) > n else lst

def analyze_oos():
    rows = read_csv_rows(OOS)
    if not rows: return None, []
    # schema: ts, rc, csv, cfg, Trades, Win%, PF, Expect, NetPnL, Fees, NetRet, MDD
    recent = tail(rows, 50)
    # strip header guesses
    def clean(r): return r if r and r[0][:4].isdigit() else None
    recent = [r for r in map(clean, recent) if r]
    pf = [as_float(r[6]) for r in recent if as_float(r[6]) is not None]
    exp = [as_float(r[7]) for r in recent if as_float(r[7]) is not None]
    win = [as_float(r[5]) for r in recent if as_float(r[5]) is not None]
    agg = {}
    if pf:
        agg["PF_avg"] = round(stats.fmean(pf), 3)
        agg["PF_med"] = round(stats.median(pf), 3)
    if exp:
        agg["Expect_avg_$"] = round(stats.fmean(exp), 2)
    if win:
        agg["Win%_avg"] = round(stats.fmean(win), 2)
    # last row
    last = recent[-1]
    last_metrics = {
        "last_ts": last[0],
        "last_rc": last[1],
        "last_trades": last[4],
        "last_win%": last[5],
        "last_pf": last[6],
        "last_expect_$": last[7],
        "last_netPnL_$": last[8],
    }
    return {"agg": agg, "last": last_metrics, "n_recent": len(recent)}, recent

def analyze_trades():
    if not TRD.exists(): return None, []
    with TRD.open("r", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
    recent = tail(rows, 200)
    exits = [row for row in rows if (row.get("action") or "").upper() == "EXIT"]
    # pnl detection
    pnl_keys = [k for k in exits[0].keys()] if exits else []
    pnl_field = next((k for k in ("pnl","PnL","pnl_net","net_pnl","profit") if k in pnl_keys), None)
    pnl_values = []
    for e in exits:
        v = e.get(pnl_field) if pnl_field else None
        try: pnl_values.append(float(v))
        except: pass
    out = {}
    if pnl_values:
        out["Exits"] = len(pnl_values)
        out["WinRate_%"] = round(100*sum(v>0 for v in pnl_values)/len(pnl_values), 2)
        out["NetPnL_$"] = round(sum(pnl_values), 2)
        out["AvgPnL_$"] = round(stats.fmean(pnl_values), 2)
        if len(pnl_values) >= 2:
            out["StdPnL_$"] = round(stats.pstdev(pnl_values), 2)
        out["PF_est"] = round((sum(v for v in pnl_values if v>0) /
                               max(1e-9, abs(sum(v for v in pnl_values if v<0)))), 3) if any(v<0 for v in pnl_values) else float("inf")
    return out, recent

def analyze_gaps():
    if not CSV.exists(): return []
    gaps = []
    with CSV.open("r") as f:
        prev_ts = None
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 1: continue
            ts = parts[0]
            try:
                # tolerate tz suffixes; only gap on wall clock seconds
                t = datetime.fromisoformat(ts.replace("Z","")).timestamp()
            except:
                prev_ts = None
                continue
            if prev_ts is not None:
                dt = t - prev_ts
                if dt > 180:  # > 3 min
                    gaps.append((ts, int(dt)))
            prev_ts = t
    return gaps

def write_csv(p: Path, rows, header=None):
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        if header: w.writerow(header)
        for r in rows: w.writerow(r)

def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    # OOS
    oos_summary, oos_recent = analyze_oos()
    if oos_recent: write_csv(OUT_OOS, oos_recent)
    # Trades
    tr_summary, tr_recent = analyze_trades()
    if tr_recent:
        write_csv(OUT_TRD, [tr_recent[0].keys()])  # header
        write_csv(OUT_TRD, [r.values() for r in tr_recent])
    # Gaps
    gaps = analyze_gaps()
    if gaps: write_csv(OUT_GAPS, gaps, header=["ts","gap_seconds"])

    # Text rollup
    with OUT_SUMMARY.open("w") as f:
        f.write("=== Tradebot Live Summary ===\n")
        now = datetime.now().isoformat(timespec="seconds")
        f.write(f"generated_at: {now}\n\n")
        if oos_summary:
            f.write("[OOS aggregates]\n")
            for k,v in oos_summary["agg"].items(): f.write(f"- {k}: {v}\n")
            f.write("[OOS last]\n")
            for k,v in oos_summary["last"].items(): f.write(f"- {k}: {v}\n")
            f.write(f"- n_recent_rows: {oos_summary['n_recent']}\n\n")
        else:
            f.write("No OOS summaries found.\n\n")
        if tr_summary:
            f.write("[Trades (from exits)]\n")
            for k,v in tr_summary.items(): f.write(f"- {k}: {v}\n")
            f.write("\n")
        else:
            f.write("No trades CSV or no exits with PnL.\n\n")
        if gaps:
            worst = max(gaps, key=lambda x:x[1])
            f.write(f"[Gaps] total_gaps: {len(gaps)} | worst_gap_sec: {worst[1]} at {worst[0]}\n")
        else:
            f.write("[Gaps] none over 3 minutes.\n")
    print(f"Wrote {OUT_SUMMARY}, {OUT_OOS if oos_recent else ''} {OUT_TRD if tr_recent else ''} {OUT_GAPS if gaps else ''}")

if __name__ == "__main__":
    main()
