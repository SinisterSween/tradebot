# scripts/run_portfolio_backtests.py
import argparse
from pathlib import Path
import shlex
import yaml
import pandas as pd

import run_backtest  # imports run_one_symbol + helpers from run_backtest.py

def load_yaml(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}

def symbol_to_csv_stem(symbol: str) -> str:
    # Must match build_universe_csvs crypto naming: BTC/USDT -> BTC_USDT
    return symbol.strip().replace("/", "_").upper()

    
def build_run_backtest_cmd(args, symbol: str, csv_path: str, universe_path: str) -> str:
    """
    Build a CLI string that mirrors what we'd run if we were calling run_backtest.py directly.
    This is only for printing/debugging; the wrapper still calls run_backtest.run_one_symbol().
    """
    cmd = [
        "PYTHONPATH=.",
        ".venv/bin/python",
        "run_backtest.py",
        "--contracts", args.contracts,
        "--profile", args.profile,
        "--universe", universe_path,
        "--symbol", symbol,
        "--csv", csv_path,
        "--csv-dir", args.csv_dir,
        "--latency-bars", "0",
        "--fee-bps", "1.0",
        "--fee-fixed", "0.0",
        "--slip-bps", "0.5",
        "--starting-equity", str(args.starting_equity),
        "--risk-pct", str(args.risk_pct),
        "--max-size", str(args.max_size),
        "--min-equity", "0.0",
    ]

    if args.env:
        cmd += ["--env", args.env]
    if args.config:
        cmd += ["--config", args.config]
    if args.no_gui:
        cmd += ["--no-gui"]

    # Join safely for shell display
    return " ".join(shlex.quote(x) for x in cmd)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-print", action="store_true", help="Pring the per-symbol backtest commands that would be executed, then exit")
    ap.add_argument("--portfolio", required=True, help="config/portfolios/mixed_1k.yaml")
    ap.add_argument("--profile", default="config/profile/equities.yaml")
    ap.add_argument("--contracts", default="config/contracts.yaml")
    ap.add_argument("--csv-dir", default="data")
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--env", default=None)
    ap.add_argument("--config", default=None)  # optional extra merge layer
    args = ap.parse_args()

    portfolio = load_yaml(args.portfolio)
    microlots = portfolio.get("microlots") or []
    if not microlots:
        raise SystemExit("No microlots found in portfolio YAML.")

    all_rows = []

    for lot in microlots:
        name = lot["name"]
        universe_name = lot["universe"]
        universe_path = f"config/universes/{universe_name}.yaml"
        universe_cfg = load_yaml(universe_path)
        symbols = [s.upper() for s in (universe_cfg.get("symbols") or [])]

        # Map portfolio -> backtest args
        bt_args = argparse.Namespace(
            contracts=args.contracts,
            profile=args.profile,
            env=args.env,
            config=args.config,
            universe=universe_path,
            all=False,
            csv_dir=args.csv_dir,
            csv=None,
            symbol=None,
            no_gui=args.no_gui,
            latency_bars=0,
            fee_bps=1.0,
            fee_fixed=0.0,
            slip_bps=0.5,
            starting_equity=float(lot.get("cash_alloc", 0.0)),
            risk_pct=float(lot.get("max_risk_per_trade", 0.0)),
            max_size=int(lot.get("max_pos", 1)),
            min_equity=0.0,
        )

        for sym in symbols:
            csv_path = str(Path(args.csv_dir) / f"{symbol_to_csv_stem(sym)}_live_1m.csv")

            if args.dry_print:
                # Print what we'd do and move on
                print(f"[DRY-PRINT] microlot={name} universe={universe_name} symbol={sym}")
                print(build_run_backtest_cmd(bt_args, sym, csv_path, universe_path))
                print()
                continue

            r = run_backtest.run_one_symbol(
                args=bt_args,
                symbol=sym,
                csv_path=csv_path,
                universe_cfg=universe_cfg,
            )
            r["Microlot"] = name
            r["Universe"] = universe_name
            r["CashAlloc"] = bt_args.starting_equity
            r["RiskPct"] = bt_args.risk_pct
            r["MaxSize"] = bt_args.max_size
            all_rows.append(r)

        if args.dry_print:
            print("[DRY-PRINT] Done. No backtests executed.")
            return
    df = pd.DataFrame(all_rows)
    print("\n=== PORTFOLIO BACKTEST SUMMARY ===")
    cols = ["Microlot","Universe","Symbol","Status","Trades","WinRate","ProfitFactor","NetPnL","CSV"]
    cols = [c for c in cols if c in df.columns]
    print(df[cols].to_string(index=False))

    ok = df[df["Status"] == "OK"] if "Status" in df.columns else df
    if not ok.empty and "NetPnL" in ok.columns:
        grp = ok.groupby(["Microlot","Universe"], as_index=False)["NetPnL"].sum()
        print("\n=== NET PnL BY MICROLOT ===")
        print(grp.to_string(index=False))
        print(f"\n[COMBINED] TotalNetPnL=${ok['NetPnL'].sum():.2f}")

if __name__ == "__main__":
    main()
