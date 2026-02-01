from dataclasses import dataclass
from typing import Optional
@dataclass
class Bracket:
    stop_price: float
    target_price: float
@dataclass
class Order:
    id: str
    ts: "pd.Timestamp"
    symbol: str
    side: str
    qty: int
    type: str
    price: Optional[float] = None
    bracket: Optional[Bracket] = None
def side_mult(side: str) -> int:
    s = (side or "").upper().strip()
    if s == "BUY":
        return 1
    if s == "SELL":
        return -1
    raise ValueError(f"bad side: {side!r}")



