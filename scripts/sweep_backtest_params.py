#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import argparse, csv, itertools, json, os, subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


def now_tag() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def find_symbol_block(universe: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """
    Return the mutable dict where per-symbol params live.

    Supports two universe YAML formats:

    Format A – list of dicts (crypto_all.yaml style):
        symbols:
          - symbol: ETH/USDT
            strategy: { ... }

    Format B – flat list of strings (sp500_fractional, tiny_cap, levered_etf, etc.):
        symbols:
          - SPY
          - QQQ
        strategy: { ... }   # top-level strategy block applies to all symbols

    In Format B we return the universe dict itself, so dot-path overrides like
    "strategy.atr_mult" land on the top-level strategy block — which is correct
    because that block controls every symbol in the flat-list format.
    """
    symbols = universe.get("symbols")
    if not isinstance(symbols, list):
        raise ValueError("Universe YAML missing top-level 'symbols:' list")

    # Format A: list of dicts
    for item in symbols:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            return item

    # Format B: flat list of strings — verify symbol exists, return top-level dict
    flat = [s for s in symbols if isinstance(s, str)]
    if symbol in flat:
        return universe   # params set directly on universe root (strategy.x, execution.x)

    raise ValueError(
        f"Symbol '{symbol}' not found in universe YAML. "
        f"Available: {[s for s in symbols if isinstance(s, str)] or [d.get('symbol') for d in symbols if isinstance(d, dict)]}"
    )


def deep_set(d: Dict[str, Any], path: str, value: Any) -> None:
    """
    Set nested dict value by dot path, e.g. "execution.be_lock_min".
    Creates intermediate dicts as needed.
    """
    keys = path.split(".")
    cur: Any = d
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def parse_param_spec(spec: str) -> Tuple[str, List[Any]]:
    """
    spec format: "execution.be_lock_min=6,8,10"
    Supports ints, floats, bools, strings.
    """
    if "=" not in spec:
        raise ValueError(f"Bad --params spec (missing '='): {spec}")
    path, raw = spec.split("=", 1)
    path = path.strip()
     # basic path validation: segments like "strategy.target_R"
    parts = path.split(".")
    if len(parts) < 2:
        raise ValueError(f"Bad --params path (need 'section.key' or deeper): {path}")
    for p in parts:
        if not p or any(ch.isspace() for ch in p):
            raise ValueError(f"Bad --params path (empty/whitespace segment): {path}")
    vals_raw = [v.strip() for v in raw.split(",") if v.strip()]

    def coerce(x: str) -> Any:
        lx = x.lower()
        if lx in ("true", "false"):
            return lx == "true"
        # int
        try:
            if x.startswith("0") and x != "0" and x.isdigit():
                # keep leading-zero strings as strings
                return x
            return int(x)
        except Exception:
            pass
        # float
        try:
            return float(x)
        except Exception:
            pass
        # string
        return x

    return path, [coerce(v) for v in vals_raw]


def safe_val(v: Any) -> str:
    if isinstance(v, bool):
        return "T" if v else "F"
    s = str(v)
    return s.replace("/", "_").replace(" ", "")


def read_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def fnum(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return float("-inf")

def run_one_job(
    *,
    repo_root: Path,
    base_uni: Dict[str, Any],
    symbol: str,
    param_paths: List[str],
    combo: Tuple[Any, ...],
    sweep_root: Path,
    run_tag: str,
    args_dict: Dict[str, Any],
) -> Dict[str, Any]:
    run_dir = sweep_root / run_tag
    run_dir.mkdir(parents=True, exist_ok=True)

    # Deep copy universe and apply overrides to *symbol block*
    uni = json.loads(json.dumps(base_uni))
    sym_block = find_symbol_block(uni, symbol)
    for path, val in zip(param_paths, combo):
        deep_set(sym_block, path, val)

    temp_universe = run_dir / "universe.yaml"
    temp_universe.write_text(yaml.safe_dump(uni, sort_keys=False))

    cmd = [
        args_dict["py_bin"], "run_backtest.py",
        "--profile", args_dict["profile"],
        "--universe", str(temp_universe),
        "--symbol", symbol,
        "--csv", args_dict["csv"],
        "--start-date", args_dict["start_date"],
        "--end-date", args_dict["end_date"],
        "--no-gui",
        "--outdir", str(sweep_root),
        "--run-tag", run_tag,
        "--light-artifacts",
    ]
    if args_dict.get("fee_bps") is not None:
        cmd += ["--fee-bps", str(args_dict["fee_bps"])]
    if args_dict.get("slip_bps") is not None:
        cmd += ["--slip-bps", str(args_dict["slip_bps"])]

    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": "."},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    (run_dir / "stdout.txt").write_text(proc.stdout)
    (run_dir / "stderr.txt").write_text(proc.stderr)

    summary = read_summary(run_dir / "summary.json")

    row: Dict[str, Any] = {
        "rc": proc.returncode,
        "run_tag": run_tag,
        "run_dir": str(run_dir.relative_to(repo_root)),
    }
    for path, val in zip(param_paths, combo):
        row[path] = val

    for k in [
        "Trades","WinRate","ProfitFactor","Expectancy","NetPnL",
        "FeesTotal","MaxDrawdown","Sharpe","Sortino",
        "NetReturnPct","GrossReturnPct","AvgTrade","AvgWin","AvgLoss","PayoffRatio","Turnover","CAGR"
    ]:
        if k in summary:
            row[k] = summary[k]

    return row



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--universe", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--start-date", required=True)
    ap.add_argument("--end-date", required=True)

    ap.add_argument("--py-bin", default=str(Path(".venv/bin/python")))
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--outdir", default="logs/sweeps")
    ap.add_argument("--jobs", type=int, default=1, help="Parallel workers (subprocesses)")
    ap.add_argument("--keep-going", action="store_true", help="Continue even if some runs fail")
    ap.add_argument("--fee-bps", type=float, default=None, help="Round-trip fee in bps (passed to run_backtest.py)")
    ap.add_argument("--slip-bps", type=float, default=None, help="Slippage in bps per side (passed to run_backtest.py)")

    ap.add_argument(
        "--params",
        action="append",
        default=[],
        help='Repeatable. Example: --params "execution.be_lock_min=6,8,10,12"'
    )

    args = ap.parse_args()
    if not args.params:
        raise ValueError("Provide at least one --params spec")

    repo_root = Path(args.repo_root).resolve()
    universe_path = (repo_root / args.universe).resolve()

    base_uni = yaml.safe_load(universe_path.read_text())
    if not isinstance(base_uni, dict):
        raise ValueError("Universe YAML did not parse to a dict")

    # Validate symbol exists
    _ = find_symbol_block(base_uni, args.symbol)

    # Parse params
    param_specs: List[Tuple[str, List[Any]]] = [parse_param_spec(s) for s in args.params]
    param_paths = [p for p, _ in param_specs]
    param_values = [vals for _, vals in param_specs]

    sweep_id = f"sweep_{args.symbol.replace('/','_')}_{now_tag()}"
    sweep_root = (repo_root / args.outdir / sweep_id).resolve()
    sweep_root.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, Any]] = []
    combos = list(itertools.product(*param_values))
    total = len(combos)

    # Build all jobs (run_tag + combo)
    jobs: List[Tuple[int, Tuple[Any, ...], str]] = []
    for i, combo in enumerate(combos, start=1):
        parts = []
        for path, val in zip(param_paths, combo):
            key = path.split(".")[-1]
            parts.append(f"{key}{safe_val(val)}")
        run_tag = "_".join(parts)
        jobs.append((i, combo, run_tag))

    args_dict = dict(
        py_bin=args.py_bin,
        profile=args.profile,
        csv=args.csv,
        start_date=args.start_date,
        end_date=args.end_date,
        fee_bps=args.fee_bps,
        slip_bps=args.slip_bps,
    )

    print(f"[SWEEP] runs={total} jobs={args.jobs} out={sweep_root.relative_to(repo_root)}")

    if args.jobs <= 1:
        # fallback: keep your old behavior but via the same worker
        for i, combo, run_tag in jobs:
            print(f"[{i}/{total}] {run_tag}")
            row = run_one_job(
                repo_root=repo_root,
                base_uni=base_uni,
                symbol=args.symbol,
                param_paths=param_paths,
                combo=combo,
                sweep_root=sweep_root,
                run_tag=run_tag,
                args_dict=args_dict,
            )
            rows.append(row)
    else:
        # parallel
        with ThreadPoolExecutor(max_workers=int(args.jobs)) as ex:
            futs = {}
            for i, combo, run_tag in jobs:
                fut = ex.submit(
                    run_one_job,
                    repo_root=repo_root,
                    base_uni=base_uni,
                    symbol=args.symbol,
                    param_paths=param_paths,
                    combo=combo,
                    sweep_root=sweep_root,
                    run_tag=run_tag,
                    args_dict=args_dict,
                )
                futs[fut] = (i, run_tag)

            completed = 0
            for fut in as_completed(futs):
                i, run_tag = futs[fut]
                completed += 1
                try:
                    row = fut.result()
                    rows.append(row)
                    print(f"[{completed}/{total}] rc={row.get('rc')} {run_tag} NetPnL={row.get('NetPnL')} Trades={row.get('Trades')}")
                except Exception as e:
                    print(f"[{completed}/{total}] FAILED {run_tag}: {e}")
                    if not args.keep_going:
                        raise


    # results.csv
    results_csv = sweep_root / "results.csv"
    keys: List[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)

    with results_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"\nDone: {results_csv.relative_to(repo_root)}")

    ok = [r for r in rows if r.get("rc") == 0]
    ranked = sorted(ok, key=lambda r: (fnum(r.get("Expectancy")), fnum(r.get("NetPnL")), -fnum(r.get("MaxDrawdown"))), reverse=True)

    print("\nTop 10:")
    for r in ranked[:10]:
        print(
            f"  {r['run_tag']} "
            f"Exp={r.get('Expectancy','')} NetPnL={r.get('NetPnL','')} "
            f"MaxDD={r.get('MaxDrawdown','')} Trades={r.get('Trades','')}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
