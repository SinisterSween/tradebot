import json, os, tempfile, shutil, threading
from dataclasses import dataclass, asdict
from typing import Optional
_LOCK = threading.Lock()
@dataclass
class OpenTradeState:
    symbol: str; side: str; qty: int; stop: float; target: float
    parent_id: int; stop_id: int; target_id: int; entry_ts_utc: str
    entry_price: float | None = None
@dataclass
class RiskState:
    realized_R: float; consec_losses: int; halted: bool
class StateStore:
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(path)
        if d and not os.path.exists(d): os.makedirs(d, exist_ok=True)
        if not os.path.isfile(path): self._atomic_write({"open_trade": None, "risk": None})
    def _atomic_write(self, obj):
        with _LOCK:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".")
            with os.fdopen(fd, "w") as f: json.dump(obj, f)
            shutil.move(tmp, self.path)
    def _read(self):
        with _LOCK:
            with open(self.path, "r") as f: return json.load(f)
    def save_open_trade(self, st: OpenTradeState):
        d = self._read(); d["open_trade"] = asdict(st); self._atomic_write(d)
    def load_open_trade(self) -> Optional[OpenTradeState]:
        data = self._read().get("open_trade"); 
        if not data: return None
        return OpenTradeState(**data)
    def clear_open_trade(self):
        d = self._read(); d["open_trade"] = None; self._atomic_write(d)
    def save_risk(self, rs: RiskState):
        d = self._read(); d["risk"] = asdict(rs); self._atomic_write(d)
    def load_risk(self) -> Optional[RiskState]:
        data = self._read().get("risk"); 
        if not data: return None
        return RiskState(**data)
