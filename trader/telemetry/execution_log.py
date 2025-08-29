import csv, os, threading
from datetime import datetime, timezone
_LOCK = threading.Lock()
_HEADERS = ["ts_utc","event","symbol","side","qty","entry_price","stop","target","exit_price","exit_reason","pnl_dollars","R","order_id","parent_id","tp_id","sl_id","notes"]
def _ensure_file(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d): os.makedirs(d, exist_ok=True)
    if not os.path.isfile(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(_HEADERS)
def log_row(path: str, row: dict):
    _ensure_file(path)
    with _LOCK, open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_HEADERS)
        w.writerow({k: row.get(k) for k in _HEADERS})
def now_str() -> str:
    return datetime.now(timezone.utc).isoformat()
def log_submit(path: str, *, symbol: str, side: str, qty: int, stop: float, target: float, parent_id: int, tp_id: int, sl_id: int, notes: str = ""):
    log_row(path, {"ts_utc": now_str(), "event": "SUBMIT", "symbol": symbol, "side": side, "qty": qty, "stop": stop, "target": target, "parent_id": parent_id, "tp_id": tp_id, "sl_id": sl_id, "notes": notes})
def log_fill(path: str, *, symbol: str, side: str, qty: int, entry_price, exit_price, exit_reason: str, pnl_dollars, R, order_id, notes: str = ""):
    log_row(path, {"ts_utc": now_str(), "event": "FILL", "symbol": symbol, "side": side, "qty": qty, "entry_price": entry_price, "exit_price": exit_price, "exit_reason": exit_reason, "pnl_dollars": pnl_dollars, "R": R, "order_id": order_id, "notes": notes})
def log_flatten(path: str, *, symbol: str, notes: str = ""):
    log_row(path, {"ts_utc": now_str(), "event": "FLATTEN", "symbol": symbol, "notes": notes})
def log_error(path: str, *, symbol: str, notes: str):
    log_row(path, {"ts_utc": now_str(), "event": "ERROR", "symbol": symbol, "notes": notes})
