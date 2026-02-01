from dataclasses import dataclass
import math
@dataclass
class RiskConfig:
    account_equity: float
    risk_pct: float
    max_daily_loss_R: float
    max_consec_losses: int
    tick_value: float
    flat_time: str
    news_lockout_minutes: int = 3
    max_dd_pct: float | None = None
    max_daily_loss_pct: float | None = None
class RiskGovernor:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.peak_equity: float | None = None
        self.day_start_equity: float | None = None
        self.realized_R = 0.0
        self.consec_losses = 0
        self.halted = False
        self.halt_reason: str | None = None
        self.reset_day()

    def reset_day(self, equity_now: float | None = None):
        self.realized_R = 0.0
        self.consec_losses = 0
        self.halted = False
        self.halt_reason = None
        if equity_now is None:
            equity_now = self.cfg.account_equity
        self.day_start_equity = float(equity_now)
        if self.peak_equity is None:
            self.peak_equity = float(equity_now)
        else:
            self.peak_equity = max(self.peak_equity, float(equity_now))
    def position_size(self, stop_distance_points: float, tick_size: float) -> int:
        """
        Contracts to trade for a stop distance in POINTS.
        Floors (never rounds up) and applies optional max_contracts_per_trade clamp.
        Works whether RiskGovernor has flat attrs or a nested cfg/config object.
        """
        if stop_distance_points <= 0 or tick_size <= 0: 
            return 0
        def _get(field, default=None):
            if hasattr(self, field):
                return getattr(self, field)
            cfg = getattr(self, "cfg", None) or getattr(self, "config", None)
            return getattr(cfg, field, default) if cfg is not None else default
        tick_value = float(_get("tick_value", 0.0))
        account_equity = float(_get("account_equity", 0.0))
        risk_pct = float(_get("risk_pct", 0.0))
        if tick_value <= 0 or account_equity <= 0 or risk_pct <= 0:
            return 0
        usd_per_point = tick_value / float(tick_size)
        per_contract = float(stop_distance_points) * usd_per_point
        if per_contract <= 0:
            return 0
        buffer = float(getattr(self, "risk_buffer_pct", 1.0) or 1.0)
        allowed = account_equity * risk_pct * buffer
        qty = math.floor(allowed / per_contract)
        mcap = getattr(self, "max_contracts_per_trade", None)
        if mcap:
            qty = min(qty, int(mcap))
        self._last_ps = {
            "usd_per_point": usd_per_point,
            "stop_dist_pts": stop_distance_points,
            "risk_per_ct": per_contract,
            "allowed": account_equity * risk_pct * float(getattr(self, "risk_buffer_pct", 1.0) or 1.0),
            "qty": qty,
            "ts": getattr(self, "_dbg_ts", None),
        }
        return max(0,qty)
    def position_size_units(self, stop_distance_points: float) -> float:
        """
        Spot sizing: returns units (float), where $PnL = units * (exit - entry).
        So risk per 1 unit for stop distance is simply stop_distance_points dollars.
        """
        if stop_distance_points <= 0:
            return 0.0

        tick_value = float(getattr(self.cfg, "tick_value", 0.0))
        account_equity = float(getattr(self.cfg, "account_equity", 0.0))
        risk_pct = float(getattr(self.cfg, "risk_pct", 0.0))
        if account_equity <= 0 or risk_pct <= 0:
            return 0.0

        buffer = float(getattr(self, "risk_buffer_pct", 1.0) or 1.0)
        allowed = account_equity * risk_pct * buffer

        # $ risk per 1 unit at this stop distance
        per_unit = float(stop_distance_points)
        if per_unit <= 0:
            return 0.0

        units = allowed / per_unit
        self._last_ps = {
            "mode": "units",
            "stop_dist_pts": stop_distance_points,
            "risk_per_unit": per_unit,
            "allowed": allowed,
            "qty": units,
            "ts": getattr(self, "_dbg_ts", None),
        }
        return max(0.0, float(units))


    def record_trade_outcome_R(self, R: float):
        self.realized_R += R
        self.consec_losses = self.consec_losses + 1 if R < 0 else 0

        if self.realized_R <= -self.cfg.max_daily_loss_R:
            self.halted = True
            self.halt_reason = f"max_daily_loss_R (realized_R={self.realized_R:.3f} <= -{self.cfg.max_daily_loss_R})"
            return

        if self.consec_losses >= self.cfg.max_consec_losses:
            self.halted = True
            self.halt_reason = f"max_consec_losses (consec_losses={self.consec_losses} >= {self.cfg.max_consec_losses})"
            return

    def can_trade_now(self, ts_local_str: str) -> bool:
        if self.halted: 
            return False
        return ts_local_str < self.cfg.flat_time
    def kill_tripped(self, equity_now: float) -> bool:
        """
        True if either:
          - Max drawdown % from peak is breached
          - Max daily loss % from today's start is breached
        """
        if equity_now is None or equity_now <= 0:
            return False
        # update peak
        if self.peak_equity is None:
            self.peak_equity = equity_now
        self.peak_equity = max(self.peak_equity, equity_now)
        # DD% from peak
        dd = 0.0 if self.peak_equity <= 0 else 1.0 - (equity_now / self.peak_equity)
        if self.cfg.max_dd_pct is not None and dd >= self.cfg.max_dd_pct:
            self.halted = True
            self.halt_reason = f"max_dd_pct (dd={dd:.3f} >= {self.cfg.max_dd_pct})"
            return True
        # Daily loss % from day start
        day_loss = 0.0
        if self.day_start_equity and self.day_start_equity > 0:
            day_loss = 1.0 - (equity_now / self.day_start_equity)
        if self.cfg.max_daily_loss_pct is not None and day_loss >= self.cfg.max_daily_loss_pct:
            self.halted = True
            self.halt_reason = f"max_daily_loss_pct (day_loss={day_loss:.3f} >= {self.cfg.max_daily_loss_pct})"
            return True
        return False
    def snapshot(self):
        return {
            "realized_R": float(self.realized_R),
            "consec_losses": int(self.consec_losses),
            "halted": bool(self.halted),
            "halt_reason": self.halt_reason,   # ✅ ADD
        }

    def restore(self, state: dict | None):
        if not state: return
        self.realized_R = float(state.get("realized_R", 0.0))
        self.consec_losses = int(state.get("consec_losses", 0))
        self.halted = bool(state.get("halted", False))
        self.halt_reason = state.get("halt_reason")  # ✅ ADD

