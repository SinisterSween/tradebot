# trader/utils/run_manifest.py
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _git_commit_hash(repo_root: Optional[str] = None) -> Optional[str]:
    try:
        cmd = ["git", "rev-parse", "HEAD"]
        out = subprocess.check_output(cmd, cwd=repo_root or None, stderr=subprocess.DEVNULL).decode().strip()
        return out
    except Exception:
        return None


def _jsonify(obj: Any) -> Any:
    # Make "anything" serializable without blowing up.
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(x) for x in obj]
    if hasattr(obj, "__dict__"):
        return _jsonify(obj.__dict__)
    return str(obj)


def write_run_manifest(
    out_dir: str | Path,
    *,
    cli_argv: list[str],
    parsed_args: Dict[str, Any],
    symbol: str,
    csv_path: str,
    start_date: Optional[str],
    end_date: Optional[str],
    resolved_config: Any,
    strategy_name: str,
    strategy_kwargs: Dict[str, Any],
    bars_info: Optional[Dict[str, Any]] = None,
    repo_root: Optional[str] = None,
) -> str:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit_hash(repo_root),
        "cli_argv": list(cli_argv),
        "parsed_args": _jsonify(parsed_args),
        "symbol": symbol,
        "csv_path": os.path.abspath(csv_path),
        "start_date": start_date,
        "end_date": end_date,
        "out_dir": str(out_dir.resolve()),
        "strategy": {
            "name": strategy_name,
            "kwargs": _jsonify(strategy_kwargs),
        },
        "resolved_config": _jsonify(resolved_config),
        "bars_info": _jsonify(bars_info or {}),
    }

    path = out_dir / "run_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return str(path)
