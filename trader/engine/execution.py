from dataclasses import dataclass
from typing import Optional
import pandas as pd
from .utils import Order, Bracket, side_mult
from .slippage import market_slippage
@dataclass
class Position:
    side: Optional[str] = None
    qty: int = 0
    avg_price: float = 0.0
class OrderManager:
    def __init__(self, commission_per_contract, exchange_fees_per_contract, tick_size,
                 fee_bps: float = 1.0, fee_fixed: float = 0.0, slip_bps: float = 0.5):
        self.commission = commission_per_contract
        self.exch_fees = exchange_fees_per_contract
        self.tick_size = tick_size
        self.pos = Position()
        self.realized_pnl = 0.0
        self.trades = []
        # new: store cost/exec knobs
        self.fee_bps   = float(fee_bps)
        self.fee_fixed = float(fee_fixed)
        self.slip_bps  = float(slip_bps)
        # track last entry fee so we net it out on exit
        self._last_entry_fee = 0.0
    # --- helpers ---
    def _notional_fee(self, price: float, qty: int) -> float:
        # bps on notional + absolute fixed per side
        return abs(price * qty) * (self.fee_bps / 10_000.0) + self.fee_fixed

    def _per_contract_fee(self, qty: int) -> float:
        # commission + exchange fees, per contract
        return (self.commission + self.exch_fees) * qty
    def _flat(self):
        self.pos = Position()
        self._last_entry_fee = 0.0
    def _round_to_tick(self, px: float) -> float:
        t = float(self.tick_size)
        return round(px / t) * t if t > 0 else px
    # --- entry path ---
    def place_and_simulate(self, bar: pd.Series, order: Order):
        # fill at bar open with slippage (latency means next bar open generally)
        if not isinstance(order.qty, (int, float)) or order.qty <= 0:
            return
        if self.pos.qty != 0:
            return
        ref_px = float(bar["open"])
        entry_px = market_slippage(ref_px, order.side, self.tick_size, bps=self.slip_bps)
        entry_px = self._round_to_tick(entry_px)
        
        # fees: notional (bps/fixed) + per-contract
        entry_fee = self._notional_fee(entry_px, order.qty) + self._per_contract_fee(order.qty)

        # update position
        self.pos.side = order.side
        self.pos.qty = int(order.qty)
        self.pos.avg_price = entry_px
        self._last_entry_fee = entry_fee

        # record trade
        self.trades.append({
            "ts": getattr(order, "ts", bar.name),
            "action": "ENTRY",
            "side": order.side,
            "qty": int(order.qty),
            "entry_price": entry_px,
            "price": entry_px,             # keep legacy field for your equity_curve()
            "fees": entry_fee,
            "notional": abs(entry_px * order.qty),
        })
    # --- exit path (stop/target) ---
    def simulate_bracket(self, bar: pd.Series, oco: Bracket):
        if self.pos.qty == 0:
            return None

        hi, lo = float(bar["high"]), float(bar["low"])
        side = self.pos.side
        qty = int(self.pos.qty)

        stop_hit   = (lo <= oco.stop_price   <= hi)
        target_hit = (lo <= oco.target_price <= hi)
        exit_price = None
        exit_reason = None

        if stop_hit and target_hit:
            exit_price, exit_reason = oco.stop_price, "STOP"
        elif stop_hit:
            exit_price, exit_reason = oco.stop_price, "STOP"
        elif target_hit:
            exit_price, exit_reason = oco.target_price, "TARGET"

        if exit_price is None:
            return None

        # slippage on exit
        exit_px = market_slippage(float(exit_price), side, self.tick_size, bps=self.slip_bps)
        exit_px = self._round_to_tick(exit_px)

        # fees on exit side
        exit_fee = self._notional_fee(exit_px, qty) + self._per_contract_fee(qty)
        total_fees = self._last_entry_fee + exit_fee

        # gross pnl (ticks * qty * direction)
        gross_pnl = (exit_px - self.pos.avg_price) * qty * side_mult(side)
        net_pnl = gross_pnl - total_fees
        self.realized_pnl += net_pnl

        # record exit trade (keep your legacy fields too)
        trade = {
            "ts": bar.name,
            "action": "EXIT",
            "reason": exit_reason,
            "side": side,
            "qty": qty,
            "entry_price": self.pos.avg_price,
            "exit_price": exit_px,
            "pnl": net_pnl,                     # net of entry+exit fees
            "fees": exit_fee,                   # exit-side fee
            "fees_total": total_fees,           # entry + exit
            "notional": abs(exit_px * qty),
        }
        self.trades.append(trade)

        # flatten
        self._flat()
        return trade
