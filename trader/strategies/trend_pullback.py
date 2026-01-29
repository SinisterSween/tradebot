# trader/strategies/trend_pullback.py
from dataclasses import dataclass
from ..engine.utils import Order, Bracket, side_mult

@dataclass
class StratConfig:
    atr_len: int
    atr_mult: float
    target_R: float
    slope_min: float
    pullback_atr: float
    stop_pad_ticks: int
    tick_size: float
    tick_value: float
    min_atr_pct: float = 0.0
    max_bar_range_atr: float = 0.0
    min_notional_usd: float = 0.0

class TrendPullback:
    def __init__(self, cfg: StratConfig):
        self.cfg = cfg

    def window_ok(self, ts_local_str: str, windows) -> bool:
        return any(w["start"] <= ts_local_str <= w["end"] for w in windows)

    def maybe_signal(self, bar, windows, risk):
        # Hard guards
        atr = float(bar.get("atr", 0.0) or 0.0)
        if atr <= 0:
            return None
        if not self.window_ok(bar["t_local"], windows):
            return None

        px = float(bar["close"])
        vwap = float(bar["vwap"])
        slope = float(bar.get("vwap_slope", 0.0) or 0.0)

        # ATR must be at least X% of price (avoid dead chop)
        if self.cfg.min_atr_pct:
            if (atr / px) < float(self.cfg.min_atr_pct):
                return None

        # Reject huge single bars (news spikes / illiquid prints)
        max_bar_range_atr = float(self.cfg.max_bar_range_atr or 0.0)
        if max_bar_range_atr > 0:
            bar_range = float(bar["high"]) - float(bar["low"])
            if atr > 0 and (bar_range / atr) > max_bar_range_atr:
                return None

        # Must be trending
        if abs(slope) < float(self.cfg.slope_min):
            return None

        pullback_band = float(self.cfg.pullback_atr) * atr
        atr_pts = float(self.cfg.atr_mult) * atr
        if atr_pts <= 0:
            return None

        side = None
        entry_price = px

        # Trend up: buy when price pulls back near/under VWAP band
        if slope > 0 and px <= vwap + pullback_band:
            side = "BUY"
            stop = entry_price - atr_pts - (self.cfg.stop_pad_ticks * self.cfg.tick_size)

        # Trend down: sell when price pulls back near/over VWAP band
        elif slope < 0 and px >= vwap - pullback_band:
            side = "SELL"
            stop = entry_price + atr_pts + (self.cfg.stop_pad_ticks * self.cfg.tick_size)

        else:
            return None

        stop_dist_pts = abs(entry_price - stop)
        qty = risk.position_size(stop_dist_pts, self.cfg.tick_size)
        if qty <= 0:
            return None

        # Min notional gate (NOW valid because qty/entry_price exist)
        min_notional_usd = float(self.cfg.min_notional_usd or 0.0)
        if min_notional_usd > 0 and (entry_price * float(qty)) < min_notional_usd:
            return None

        target_pts = float(self.cfg.target_R) * stop_dist_pts
        target = entry_price + target_pts * side_mult(side)

        order_id = str(bar.get("ts") or bar.get("datetime") or bar.get("t_utc") or bar.get("timestamp") or bar.name)
        order = Order(
            id=order_id,
            ts=bar.name,          # bar index (DatetimeIndex in your runs)
            symbol=bar["symbol"],
            side=side,
            qty=qty,
            type="MARKET",
        )
        bracket = Bracket(stop_price=stop, target_price=target)

        return {"order": order, "bracket": bracket, "stop_dist_points": stop_dist_pts}
