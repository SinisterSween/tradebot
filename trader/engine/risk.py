from dataclasses import dataclass
@dataclass
class RiskConfig:
    account_equity: float
    risk_pct: float
    max_daily_loss_R: float
    max_consec_losses: int
    tick_value: float
    flat_time: str
    news_lockout_minutes: int = 3
class RiskGovernor:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.reset_day()
    def reset_day(self):
        self.realized_R = 0.0
        self.consec_losses = 0
        self.halted = False
    def position_size(self, stop_distance_points: float, tick_size: float) -> int:
        import math
        if stop_distance_points <= 0: return 0
        risk_dollars = self.cfg.account_equity * self.cfg.risk_pct
        stop_ticks = stop_distance_points / tick_size
        risk_per_contract = max(1.0, stop_ticks) * self.cfg.tick_value
        qty = math.floor(risk_dollars / risk_per_contract)
        return max(0, qty)
    def record_trade_outcome_R(self, R: float):
        self.realized_R += R
        self.consec_losses = self.consec_losses + 1 if R < 0 else 0
        if self.realized_R <= -self.cfg.max_daily_loss_R or self.consec_losses >= self.cfg.max_consec_losses:
            self.halted = True
    def can_trade_now(self, ts_local_str: str) -> bool:
        if self.halted: return False
        return ts_local_str < self.cfg.flat_time
    def snapshot(self):
        return {"realized_R": float(self.realized_R), "consec_losses": int(self.consec_losses), "halted": bool(self.halted)}
    def restore(self, state: dict | None):
        if not state: return
        self.realized_R = float(state.get("realized_R", 0.0))
        self.consec_losses = int(state.get("consec_losses", 0))
        self.halted = bool(state.get("halted", False))
