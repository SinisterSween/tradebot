# trader/strategies/vwap_reversion.py
from dataclasses import dataclass
from ..engine.utils import Order, Bracket, side_mult
import math

@dataclass
class StratConfig:
    atr_len: int
    atr_mult: float          # how far from VWAP in ATR units before entering
    target_R: float          # R multiple target (fallback if not targeting VWAP)
    slope_max: float         # only trade if abs(vwap_slope) <= slope_max (mean-reversion regime)
    stop_pad_ticks: int
    tick_size: float
    tick_value: float
    min_atr_pct: float = 0.0
    min_vwap_target_R: float = 0.0
    target_vwap: bool = True
    max_bar_range_atr: float = 0.0
    ema_slope_max: float = 0.0
    ema_max_dist_atr: float = 0.0
    vwap_entry_min_atr: float = 0.0
    vwap_entry_max_atr: float = 0.0

class VwapReversion:
    def __init__(self, cfg: StratConfig):
        self.cfg = cfg

    def window_ok(self, ts_local_str: str, windows) -> bool:
        return any(w["start"] <= ts_local_str <= w["end"] for w in windows)

    def maybe_signal(self, bar, windows, risk):
        if bar["atr"] <= 0:
            return None
        if not self.window_ok(bar["t_local"], windows):
            return None

        px = float(bar.get("close", 0.0) or 0.0)
        atr = float(bar.get("atr", 0.0) or 0.0)
        vwap_raw = bar.get("vwap", px)

        try:
            vwap = float(vwap_raw)
        except Exception:
            vwap = px

        # hard guards (prevents NaN vwap from killing every signal)
        if px <= 0 or atr <= 0:
            return None
        if not math.isfinite(px) or not math.isfinite(atr):
            return None
        if (not math.isfinite(vwap)) or vwap <= 0:
            vwap = px

        dist_atr = abs(px - vwap) / atr

        if float(getattr(self.cfg, "vwap_entry_min_atr", 0.0) or 0.0) > 0 and dist_atr < float(self.cfg.vwap_entry_min_atr):
            return None
        if float(getattr(self.cfg, "vwap_entry_max_atr", 0.0) or 0.0) > 0 and dist_atr > float(self.cfg.vwap_entry_max_atr):
            return None

        # dead / micro chop
        if self.cfg.min_atr_pct and (atr / px) < float(self.cfg.min_atr_pct):
            return None

        # reject huge bars
        if self.cfg.max_bar_range_atr:
            bar_range = float(bar["high"]) - float(bar["low"])
            if (bar_range / atr) > float(self.cfg.max_bar_range_atr):
                return None

        # mean reversion regime: VWAP slope must be flat-ish
        slope = float(bar.get("vwap_slope", 0.0) or 0.0)
        if abs(slope) > float(self.cfg.slope_max):
            return None

        # optional EMA regime gate (only if you actually compute ema_slope)
        if float(getattr(self.cfg, "ema_slope_max", 0.0) or 0.0) > 0:
            ema_slope = float(bar.get("ema_slope", 0.0) or 0.0)
            if abs(ema_slope) > float(self.cfg.ema_slope_max):
                return None
            
        if float(getattr(self.cfg, "ema_max_dist_atr", 0.0) or 0.0) > 0:
            ema_dist_atr = float(bar.get("ema_dist_atr", 0.0) or 0.0)
            if abs(ema_dist_atr) > float(self.cfg.ema_max_dist_atr):
                return None

        atr_pts = float(self.cfg.atr_mult) * atr
        if atr_pts <= 0:
            return None

        side = None
        entry_price = px

        if px >= vwap + atr_pts:
            side = "SELL"
            stop = entry_price + atr_pts + self.cfg.stop_pad_ticks * self.cfg.tick_size
            target = vwap if self.cfg.target_vwap else entry_price - self.cfg.target_R * abs(entry_price - stop)
            if self.cfg.target_vwap and float(getattr(self.cfg, "min_vwap_target_R", 0.0) or 0.0) > 0:
                stop_dist = abs(entry_price - stop)
                tgt_dist = abs(vwap - entry_price)
                if stop_dist <= 0 or (tgt_dist / stop_dist) < float(self.cfg.min_vwap_target_R):
                    return None

        elif px <= vwap - atr_pts:
            side = "BUY"
            stop = entry_price - atr_pts - self.cfg.stop_pad_ticks * self.cfg.tick_size
            target = vwap if self.cfg.target_vwap else entry_price + self.cfg.target_R * abs(entry_price - stop)
            if self.cfg.target_vwap and float(getattr(self.cfg, "min_vwap_target_R", 0.0) or 0.0) > 0:
                stop_dist = abs(entry_price - stop)
                tgt_dist = abs(vwap - entry_price)
                if stop_dist <= 0 or (tgt_dist / stop_dist) < float(self.cfg.min_vwap_target_R):
                    return None

        else:
            return None

        order_id = str(bar.get("ts") or bar.get("datetime") or bar.get("t_utc") or bar.get("timestamp") or bar.name)

        order = Order(
            id=order_id,
            ts=bar.name,
            symbol=bar["symbol"],
            side=side,
            qty=1,          # runner will size this
            type="MARKET",
        )
        bracket = Bracket(stop_price=stop, target_price=target)
        return {"order": order, "bracket": bracket, "stop_dist_points": abs(entry_price - stop)}

