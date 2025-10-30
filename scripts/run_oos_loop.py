#!/usr/bin/env python3
import subprocess, time, argparse, datetime as dt, os, sys, json, csv, shutil
from pathlib import Path

def run(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    return p.returncode, out

def safe_int(x, default=0):
    try: return int(x)
    except: return default

def read_summary(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def csv_rows(path):
    try:
        with open(path, "r") as f:
            # cheap line count without pandas
            return sum(1 for _ in f) - 1  # minus header
    except Exception:
        return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cadence-min", type=int, default=10, help="minutes between backtests")
    ap.add_argument("--csv", default="data/MES_live_1m.csv")
    ap.add_argument("--config", default="config/overrides/clean_v1_be10_tick1.yaml")
    ap.add_argument("--contracts", default="config/contracts.yaml")
    ap.add_argument("--profile", default="config/profile/futures.yaml")
    ap.add_argument("--env", default="config/env/paper.yaml")
    ap.add_argument("--symbol", default="MES")
    ap.add_argument("--fees", nargs=3, type=float, default=[0,0,0], metavar=("FEE_BPS","SLIP_BPS","FEE_FIXED"))
    ap.add_argument("--min-bars", type=int, default=150, help="skip run if fewer than this many rows in CSV")
    ap.add_argument("--once", action="store_true", help="run a single backtest then exit")
    ap.add_argument("--log", default="logs/oos_log.csv", help="Where to append OOS summary rows (per-symbol).")
    ap.add_argument("--trades-csv", default="logs/backtest_trades.csv", help="Per-symbol trades CSV to write/overwrite each run.")
    ap.add_argument("--tag", default="MES", help="Symbol/tag used in filenames or summary labeling.")
    args = ap.parse_args()

    os.makedirs("logs", exist_ok=True)
    loop_log = "logs/oos_loop.out"
    oos_csv  = args.log
    trades_out = Path(args.trades_csv)
    trades_out.parent.mkdir(parents=True, exist_ok=True)

    last_mtime = 0.0

    # ensure oos_log.csv has a header
    if not os.path.exists(oos_csv):
        with open(oos_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "ts","rc","csv","config",
                "Trades","WinRate","ProfitFactor","Expectancy",
                "NetPnL","FeesTotal","NetReturnPct","MaxDrawdown"
            ])

    while True:
        try:
            mtime = os.path.getmtime(args.csv)
        except FileNotFoundError:
            mtime = 0.0

        if mtime <= last_mtime:
            with open(loop_log, "a") as f:
                f.write(f"[SKIP] {dt.datetime.now().isoformat(timespec='seconds')} — CSV unchanged; sleeping\n")
            time.sleep(max(60, args.cadence_min * 60))
            continue
        last_mtime = mtime

        start = dt.datetime.now().isoformat(timespec="seconds")

        # Skip if CSV is missing or too small
        rows = csv_rows(args.csv)
        if rows < args.min_bars:
            line = f"[SKIP] {start} — rows={rows} < min_bars={args.min_bars} — {args.csv}\n"
            with open(loop_log, "a") as f:
                f.write(line)
            # cadence sleep
            time.sleep(max(60, args.cadence_min * 60))
            if args.once:
                break
            continue

        cmd = [
            sys.executable, "run_backtest.py",
            "--contracts", args.contracts,
            "--profile", args.profile,
            "--env", args.env,
            "--symbol", args.symbol,
            "--csv", args.csv,
            "--no-gui",
            "--fee-bps", str(args.fees[0]),
            "--slip-bps", str(args.fees[1]),
            "--fee-fixed", str(args.fees[2]),
            "--config", args.config
        ]

        rc, out = run(cmd)

        # Always append raw output to loop log
        with open(loop_log, "a") as f:
            f.write(f"[RUN] {start} — rc={rc} — {args.csv} — {args.config}\n")
            f.write(out + ("" if out.endswith("\n") else "\n"))

        # Try to pull summary.json -> oos_log.csv
        summary = read_summary("logs/summary.json")
        row = [
            start, rc, args.csv, args.config,
            safe_int(summary.get("Trades")),
            f"{summary.get('WinRate', 0):.4f}" if isinstance(summary.get("WinRate"), (int,float)) else summary.get("WinRate"),
            summary.get("ProfitFactor"),
            summary.get("Expectancy"),
            summary.get("NetPnL"),
            summary.get("FeesTotal"),
            summary.get("NetReturnPct"),
            summary.get("MaxDrawdown"),
        ]
        with open(oos_csv, "a", newline="") as f:
            csv.writer(f).writerow(row)

        # after the run, we may have logs/trades_<tag>.csv; copy/rename to the requested trades-csv
        src_trades = Path(f"logs/backtest_trades_{args.tag}.csv")
        if src_trades.exists():
            dst = Path(args.trades_csv)
            if src_trades.resolve() != dst.resolve():
                try:
                    dst.write_text(src_trades.read_text())
                    print(f"Saved trades to {dst}")
                except Exception as e:
                    print(f"[WARN] failed to copy trades to CSV to {dst}: {e}")
            else:
                # same file, don't warn
                pass


        # cadence sleep
        time.sleep(max(60, args.cadence_min * 60))
        if args.once:
            break

if __name__ == "__main__":
    main()
