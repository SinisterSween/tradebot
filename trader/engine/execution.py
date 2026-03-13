from dataclasses import dataclass
from typing import Optional
import pandas as pd
import numpy as np
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
        EPS = 1e-12

        # Already in a position?
        try:
            pos_qty = float(getattr(self.pos, "qty", 0.0) or 0.0)
        except Exception:
            pos_qty = 0.0
        if abs(pos_qty) > EPS:
            return

        # qty
        try:
            qty = float(getattr(order, "qty", 0.0) or 0.0)
        except Exception:
            return
        if not np.isfinite(qty) or qty <= EPS:
            return

        # side
        side = str(getattr(order, "side", "")).upper().strip()
        if side not in {"BUY", "SELL"}:
            return

        # ref price
        try:
            ref_px = float(bar.get("open", bar.get("close")))
        except Exception:
            return
        if not np.isfinite(ref_px) or ref_px <= 0:
            return

        fill_px = market_slippage(ref_px, side, self.tick_size, bps=self.slip_bps)
        fill_px = self._round_to_tick(float(fill_px))

        if not np.isfinite(fill_px) or fill_px <= 0:
            return

        entry_fee = self._notional_fee(fill_px, qty) + self._per_contract_fee(qty)
        self._last_entry_fee = float(entry_fee)

        # update position
        self.pos.side = side
        self.pos.qty = float(qty)
        self.pos.avg_price = float(fill_px)

        # notional
        if getattr(self, "crypto_like", False) or getattr(self, "equity_like", False):
            notional = abs(float(fill_px) * float(qty))
        else:
            notional = abs(float(fill_px) * float(self.dpp) * float(qty))

        ts = getattr(order, "ts", None)
        if ts is None:
            ts = bar.get("ts") or bar.get("datetime") or bar.get("t_utc") or bar.get("timestamp") or bar.name

        self.trades.append({
            "ts": ts,
            "action": "ENTRY",
            "side": side,
            "qty": float(qty),
            "entry_price": float(fill_px),
            "price": float(fill_px),
            "fees": float(entry_fee),
            "fees_total": float(entry_fee),
            "notional": float(notional),
        })



    # --- exit path (stop/target) ---
    def simulate_bracket(self, bar: pd.Series, oco: Bracket):
        EPS = 1e-12

        # position must be open
        try:
            pos_qty = float(getattr(self.pos, "qty", 0.0) or 0.0)
        except Exception:
            return None
        if not np.isfinite(pos_qty) or abs(pos_qty) <= EPS:
            return None

        hi = float(bar.get("high", bar.get("close", 0.0)))
        lo = float(bar.get("low",  bar.get("close", 0.0)))
        close_px = float(bar.get("close", (hi + lo) / 2.0))
        if not (np.isfinite(hi) and np.isfinite(lo) and np.isfinite(close_px)):
            return None

        side_u = str(getattr(self.pos, "side", "")).upper().strip()
        if side_u not in {"BUY", "SELL"}:
            return None

        qty = abs(pos_qty)
        entry_px = float(getattr(self.pos, "avg_price", close_px))
        stop_px = float(getattr(oco, "stop_price", float("nan")))
        tgt_px  = float(getattr(oco, "target_price", float("nan")))
        if not (np.isfinite(entry_px) and np.isfinite(stop_px) and np.isfinite(tgt_px)):
            return None

        stop_hit_intra   = (lo <= stop_px <= hi)
        target_hit_intra = (lo <= tgt_px  <= hi)

        if side_u == "BUY":
            stop_hit_close   = close_px <= stop_px
            target_hit_close = close_px >= tgt_px
        else:  # SELL
            stop_hit_close   = close_px >= stop_px
            target_hit_close = close_px <= tgt_px

        stop_hit   = stop_hit_intra   or stop_hit_close
        target_hit = target_hit_intra or target_hit_close

        if stop_hit and target_hit:
            exit_price, exit_reason = stop_px, "STOP"
        elif stop_hit:
            exit_price, exit_reason = stop_px, "STOP"
        elif target_hit:
            exit_price, exit_reason = tgt_px, "TARGET"
        else:
            return None

        # exit side is opposite of position side
        exit_side = "SELL" if side_u == "BUY" else "BUY"

        exit_px = market_slippage(float(exit_price), exit_side, self.tick_size, bps=self.slip_bps)
        exit_px = self._round_to_tick(float(exit_px))
        if not np.isfinite(exit_px):
            return None

        exit_fee = self._notional_fee(exit_px, qty) + self._per_contract_fee(qty)
        total_fees = float(getattr(self, "_last_entry_fee", 0.0)) + float(exit_fee)

        if getattr(self, "crypto_like", False) or getattr(self, "equity_like", False):
            gross_pnl = (exit_px - float(entry_px)) * qty * side_mult(side_u)
        else:
            gross_pnl = (exit_px - float(entry_px)) * float(self.dpp) * qty * side_mult(side_u)
        net_pnl = gross_pnl - total_fees
        self.realized_pnl += net_pnl

        trade = {
            "ts": bar.name,
            "action": "EXIT",
            "reason": exit_reason,
            "side": side_u,
            "exit_side": exit_side,
            "qty": float(qty),
            "price": float(entry_px),
            "entry_price": float(entry_px),
            "exit_price": float(exit_px),
            "pnl": float(net_pnl),
            "fees_total": float(total_fees),
            "notional": abs(exit_px * qty) if (getattr(self, "crypto_like", False) or getattr(self, "equity_like", False))
                        else abs(exit_px * self.dpp * qty),
        }
        self.trades.append(trade)

        # flatten + clear fee carry
        self.pos.qty = 0.0
        self.pos.side = None
        self.pos.avg_price = 0.0
        self._last_entry_fee = 0.0

        return trade


