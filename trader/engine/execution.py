from dataclasses import dataclass
from typing import Optional
import pandas as pd
from .utils import Order, Bracket, side_mult
from .fees import per_side_cost
from .slippage import market_slippage
@dataclass
class Position:
    side: Optional[str] = None
    qty: int = 0
    avg_price: float = 0.0
class OrderManager:
    def __init__(self, commission_per_contract: float, exchange_fees_per_contract: float, tick_size: float):
        self.commission = commission_per_contract
        self.exch_fees = exchange_fees_per_contract
        self.tick_size = tick_size
        self.pos = Position()
        self.realized_pnl = 0.0
        self.trades = []
    def _flat(self):
        self.pos = Position()
    def place_and_simulate(self, bar: pd.Series, order: Order):
        entry_px = market_slippage(bar["open"], order.side, self.tick_size)
        entry_cost = per_side_cost(self.commission, self.exch_fees, order.qty)
        self.pos.side = order.side
        self.pos.qty = order.qty
        self.pos.avg_price = entry_px
        self.trades.append({"ts": order.ts, "action": "ENTRY", "side": order.side, "qty": order.qty, "price": entry_px, "fees": entry_cost})
    def simulate_bracket(self, bar: pd.Series, oco: Bracket):
        if self.pos.qty == 0: return None
        hi, lo = bar["high"], bar["low"]
        side = self.pos.side; qty = self.pos.qty
        stop_hit = (lo <= oco.stop_price <= hi)
        target_hit = (lo <= oco.target_price <= hi)
        exit_price = None; exit_reason = None
        if stop_hit and target_hit:
            exit_price = oco.stop_price; exit_reason = "STOP"
        elif stop_hit:
            exit_price = oco.stop_price; exit_reason = "STOP"
        elif target_hit:
            exit_price = oco.target_price; exit_reason = "TARGET"
        if exit_price is not None:
            fees = per_side_cost(self.commission, self.exch_fees, qty)
            pnl = (exit_price - self.pos.avg_price) * qty * side_mult(side)
            self.realized_pnl += pnl - fees
            exit = {"ts": bar.name, "action": "EXIT", "reason": exit_reason, "side": side, "qty": qty,
                    "entry_price": self.pos.avg_price, "exit_price": exit_price, "pnl": pnl, "fees": fees}
            self.trades.append(exit)
            self._flat()
            return exit
        return None
