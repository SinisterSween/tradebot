#!/usr/bin/env python3
from __future__ import annotations

import argparse, csv, itertools, json, os, subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


def now_tag() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def find_symbol_block(universe: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    symbols = universe.get("symbols")
    if not isinstance(symbols, list):
        raise ValueError("Universe YAML missing top-level 'symbols:' list")
    for item in symbols:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            return item
    raise ValueError(f"Symbol '{symbol}' not found in universe YAML")


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

    for i, combo in enumerate(combos, start=1):
        # Build run tag: key=val_key=val...
        parts = []
        for path, val in zip(param_paths, combo):
            key = path.split(".")[-1]
            parts.append(f"{key}{safe_val(val)}")
        run_tag = "_".join(parts)

        run_dir = sweep_root / run_tag
        run_dir.mkdir(parents=True, exist_ok=True)

        # Deep copy universe and apply overrides to *symbol block*
        uni = json.loads(json.dumps(base_uni))
        sym_block = find_symbol_block(uni, args.symbol)

        for path, val in zip(param_paths, combo):
            deep_set(sym_block, path, val)

        temp_universe = run_dir / "universe.yaml"
        temp_universe.write_text(yaml.safe_dump(uni, sort_keys=False))

        cmd = [
            args.py_bin, "run_backtest.py",
            "--profile", args.profile,
            "--universe", str(temp_universe),
            "--symbol", args.symbol,
            "--csv", args.csv,
            "--start-date", args.start_date,
            "--end-date", args.end_date,
            "--no-gui",
            "--outdir", str(sweep_root),
            "--run-tag", run_tag,
            "--light-artifacts",
        ]

        print(f"[{i}/{total}] {run_tag}")
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

        summary_path = run_dir / "summary.json"
        summary = read_summary(summary_path)

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

        rows.append(row)

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
