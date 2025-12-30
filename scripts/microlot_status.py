#!/usr/bin/env python3
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        # show local-ish compact string
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def fmt_float(x: Any, digits: int = 2) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "-"


def load_states() -> List[Dict[str, Any]]:
    state_dir = Path("state")
    if not state_dir.exists():
        return []

    rows: List[Dict[str, Any]] = []
    for fp in sorted(state_dir.glob("*.json")):
        try:
            with fp.open("r") as f:
                s = json.load(f)
        except Exception as e:
            print(f"[WARN] failed to load {fp}: {e}")
            continue

        rows.append(
            {
                "name": s.get("name", fp.stem),
                "universe": s.get("universe", ""),
                "symbol": s.get("symbol", ""),
                "cash": s.get("cash_alloc", 0.0),
                "risk": s.get("max_risk_per_trade", 0.0),
                "trades": s.get("trade_count", 0),
                "last_side": s.get("last_side") or "-",
                "last_qty": s.get("last_qty"),
                "last_decision": fmt_ts(s.get("last_decision_ts")),
                "heartbeat": fmt_ts(s.get("last_heartbeat_ts")),
            }
        )
    return rows


def print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("No microlot state files found under state/")
        return

    headers = [
        "NAME",
        "UNIVERSE",
        "SYMBOL",
        "CASH",
        "RISK%",
        "TRADES",
        "LAST_SIDE",
        "LAST_QTY",
        "LAST_DECISION",
        "HEARTBEAT",
    ]

    def row_to_list(r: Dict[str, Any]):
        return [
            r["name"],
            r["universe"],
            r["symbol"],
            fmt_float(r["cash"]),
            fmt_float(float(r["risk"]) * 100.0, digits=2),
            str(r["trades"]),
            r["last_side"],
            fmt_float(r["last_qty"]) if r["last_qty"] is not None else "-",
            r["last_decision"],
            r["heartbeat"],
        ]

    data = [row_to_list(r) for r in rows]

    # compute column widths
    widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # print header
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = "  ".join("-" * widths[i] for i in range(len(headers)))
    print(header_line)
    print(sep_line)

    # print rows
    for row in data:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def main() -> None:
    rows = load_states()
    print_table(rows)


if __name__ == "__main__":
    main()
