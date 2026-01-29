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
                 fee_bps: float = 1.0, fee_fixed: float = 0.0, slip_bps: float = 0.5,
                 dollars_per_point: float = 1.0):
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
        self.dpp = float(dollars_per_point)
    # --- helpers ---
    def _notional_fee(self, price: float, qty: int) -> float:
        # bps on notional + absolute fixed per side
        dollar_notional = abs(price * self.dpp * qty)
        return dollar_notional * (self.fee_bps / 10_000.0) + self.fee_fixed

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
        if getattr(self.pos, "qty", 0) != 0:
            return
        try:
            qty = float(order.qty)
        except Exception:
            return
        if qty <= 0:
            return
        
        side = str(order.side).upper()

        # fill at bar open with slippage (next bar open sim)
        ref_px = float(bar["open"])
        entry_px = market_slippage(ref_px, side, self.tick_size, bps=self.slip_bps)
        entry_px = self._round_to_tick(entry_px)

        # fees: notional (bps/fixed) + per-contract
        entry_fee = self._notional_fee(entry_px, qty) + self._per_contract_fee(qty)
        self._last_entry_fee = entry_fee

        # update position (IMPORTANT: keep crypto/equity qty as float)
        self.pos.side = side
        self.pos.qty = qty
        self.pos.avg_price = entry_px

        # notional for logging
        if getattr(self, "crypto_like", False) or getattr(self, "equity_like", False):
            notional = abs(entry_px * qty)
        else:
            notional = abs(entry_px * self.dpp * qty)
        # record trade
        ts = getattr(order, "ts", None)
        if ts is None:
            ts = bar.get("ts") or bar.get("datetime") or bar.get("t_utc") or bar.get("timestamp") or bar.name

        self.trades.append({
            "ts": ts,
            "action": "ENTRY",
            "side": self.pos.side,
            "qty": float(qty),
            "entry_price": entry_px,
            "price": entry_px,             # keep legacy field for equity_curve()
            "fees": float(entry_fee),
            "fees_total": float(entry_fee),  # handy for consistency (optional but recommended)
            "notional": float(notional),
        })  

    # --- exit path (stop/target) ---
    def simulate_bracket(self, bar: pd.Series, oco: Bracket):
        if float(self.pos.qty) <= 0:
            return None

        hi = float(bar.get("high", bar["close"]))
        lo = float(bar.get("low",  bar["close"]))
        close_px = float(bar.get("close", (hi + lo) / 2.0))

        side_u = str(self.pos.side).upper()
        qty = float(self.pos.qty)  # IMPORTANT: keep crypto qty as float
        entry_px = float(self.pos.avg_price)
        stop_px = float(oco.stop_price)
        tgt_px  = float(oco.target_price)

        # Intrabar hit (classic)
        stop_hit_intra   = (lo <= stop_px <= hi)
        target_hit_intra = (lo <= tgt_px  <= hi)

        # Close-based fallback (handles flat OHLC bars)
        if side_u == "BUY":
            stop_hit_close   = close_px <= stop_px
            target_hit_close = close_px >= tgt_px
        else:  # SELL
            stop_hit_close   = close_px >= stop_px
            target_hit_close = close_px <= tgt_px

        stop_hit   = stop_hit_intra   or stop_hit_close
        target_hit = target_hit_intra or target_hit_close

        exit_price = None
        exit_reason = None

        if stop_hit and target_hit:
            exit_price, exit_reason = stop_px, "STOP"
        elif stop_hit:
            exit_price, exit_reason = stop_px, "STOP"
        elif target_hit:
            exit_price, exit_reason = tgt_px, "TARGET"
        if exit_price is None:
            return None

        # slippage on exit
        exit_px = market_slippage(float(exit_price), side_u, self.tick_size, bps=self.slip_bps)
        exit_px = self._round_to_tick(exit_px)

        # fees on exit side
        exit_fee = self._notional_fee(exit_px, qty) + self._per_contract_fee(qty)
        total_fees = float(getattr(self, "_last_entry_fee", 0.0)) + float(exit_fee)

        # gross pnl (ticks * qty * direction)
        if getattr(self, "crypto_like", False) or getattr(self, "equity_like", False):
            gross_pnl = (exit_px - float(entry_px)) * qty * side_mult(side_u)
        else:
            gross_pnl = (exit_px - float(entry_px)) * float(self.dpp) * qty * side_mult(side_u)
        net_pnl = gross_pnl - total_fees
        self.realized_pnl += net_pnl

        # record exit trade (keep your legacy fields too)
        trade = {
            "ts": bar.name,
            "action": "EXIT",
            "reason": exit_reason,
            "side": side_u,
            "qty": float(qty),
            "price": float(entry_px),
            "entry_price": float(entry_px),
            "exit_price": float(exit_px),
            "pnl": float(net_pnl),                                      
            "fees_total": float(total_fees),           # entry + exit
            "notional": abs(exit_px * qty) if (getattr(self, "crypto_like", False) or getattr(self, "equity_like", False))
                        else abs(exit_px * self.dpp * qty),
        }
        self.trades.append(trade)

        return trade
