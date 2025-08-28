from dataclasses import dataclass
from ..engine.utils import Order, Bracket, side_mult
@dataclass
class StratConfig:
    orb_minutes: int
    atr_len: int
    break_eps_ticks: int
    atr_mult: float
    target_R: float
    slope_min: float
    stop_pad_ticks: int
    trail_pad_ticks: int
    tick_size: float
    tick_value: float
class HybridOrbVwap:
    def __init__(self, cfg: StratConfig):
        self.cfg = cfg
    def window_ok(self, ts_local_str: str, windows) -> bool:
        return any(w["start"] <= ts_local_str <= w["end"] for w in windows)
    def maybe_signal(self, bar, windows, risk):
        if bar["atr"] <= 0: return None
        if not self.window_ok(bar["t_local"], windows): return None
        eps = self.cfg.break_eps_ticks * self.cfg.tick_size
        atr_pts = self.cfg.atr_mult * bar["atr"]
        side = None; entry_price = None
        if bar["close"] >= bar["orh"] + eps and bar["close"] >= bar["vwap"] and bar["vwap_slope"] >= self.cfg.slope_min:
            side = "BUY"; entry_price = bar["close"]
            stop = min(bar["orl"] - self.cfg.stop_pad_ticks*self.cfg.tick_size, entry_price - atr_pts)
        elif bar["close"] <= bar["orl"] - eps and bar["close"] <= bar["vwap"] and bar["vwap_slope"] <= -self.cfg.slope_min:
            side = "SELL"; entry_price = bar["close"]
            stop = max(bar["orh"] + self.cfg.stop_pad_ticks*self.cfg.tick_size, entry_price + atr_pts)
        else:
            return None
        stop_dist_pts = abs(entry_price - stop)
        qty = risk.position_size(stop_dist_pts, self.cfg.tick_size)
        if qty <= 0: return None
        target_pts = self.cfg.target_R * stop_dist_pts
        target = entry_price + target_pts * side_mult(side)
        order = Order(id=f"{bar.name.value}", ts=bar.name, symbol=bar["symbol"], side=side, qty=qty, type="MARKET")
        bracket = Bracket(stop_price=stop, target_price=target)
        return {"order": order, "bracket": bracket, "stop_dist_points": stop_dist_pts}
