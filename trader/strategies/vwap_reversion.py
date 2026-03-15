# trader/strategies/vwap_reversion.py
from dataclasses import dataclass
from ..engine.utils import Order, Bracket, side_mult
import math
from collections import Counter

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
    orb_minutes: int = 0
    # Directional regime gates (separate from symmetric ema_slope_max)
    allow_longs: bool = True    # set False to disable BUY signals entirely
    allow_shorts: bool = True   # set False to disable SELL signals entirely
    ema_slope_buy_min: float = -999.0   # block BUY when ema_slope_atr < this (default: off)
    ema_slope_sell_max: float = 999.0   # block SELL when ema_slope_atr > this (default: off)

class VwapReversion:
    def __init__(self, cfg: StratConfig, *, debug_cfg_echo: bool = False):
        self.cfg = cfg
        self._why = Counter()
        self._rej = Counter()
        self._last_trade_ts = None          # pd.Timestamp
        self._day = None                    # datetime.date
        self._trades_today = 0
        if debug_cfg_echo:
            print(f"[STRAT.ECHO] VwapReversion cfg={self.cfg}")


    def on_entry_submitted(self, ts) -> None:
        self._trades_today += 1
        self._last_trade_ts = ts

    def _reset_if_new_day(self, bar):
        d = bar.get("date", None)
        if d is None:
            return
        if self._day != d:
            self._day = d
            self._trades_today = 0
            self._last_trade_ts = None

    def _minutes_since_last(self, ts_now) -> float:
        if self._last_trade_ts is None or ts_now is None:
            return 1e9
        # both should be tz-aware timestamps
        delta = ts_now - self._last_trade_ts
        return delta.total_seconds() / 60.0


    def window_ok(self, ts_local_str: str, windows) -> bool:
        return any(w["start"] <= ts_local_str <= w["end"] for w in windows)

    def maybe_signal(self, bar, windows, risk):
        def reject(reason: str):
            self._rej[reason] += 1
            return None
        
        self._reset_if_new_day(bar)

        if not self.window_ok(bar["t_local"], windows):
            return reject("window")

        atr = float(bar.get("atr", 0.0) or 0.0)
        if atr <= 0:
            return reject("atr<=0")
        
        if self.cfg.orb_minutes > 0 and not bool(bar.get("orb_ok", True)):
            return reject("skip_orb")

        px = float(bar.get("close", 0.0) or 0.0)
        atr = float(bar.get("atr", 0.0) or 0.0)
        vwap_raw = bar.get("vwap", px)

        try:
            vwap = float(vwap_raw)
        except Exception:
            vwap = px

        # hard guards (prevents NaN vwap from killing every signal)
        if px <= 0 or atr <= 0:
            return reject("px_or_atr<=0")
        if not math.isfinite(px) or not math.isfinite(atr):
            return reject("nonfinite_price_or_atr")
        if (not math.isfinite(vwap)) or vwap <= 0:
            vwap = px

        dist_atr = abs(px - vwap) / atr

        vmin = float(getattr(self.cfg, "vwap_entry_min_atr", 0.0) or 0.0)
        if vmin > 0 and dist_atr < vmin:
            return reject("vwap_entry_min_atr")

        vmax = float(getattr(self.cfg, "vwap_entry_max_atr", 0.0) or 0.0)
        if vmax > 0 and dist_atr > vmax:
            return reject("vwap_entry_max_atr")

        # dead / micro chop
        if self.cfg.min_atr_pct and (atr / px) < float(self.cfg.min_atr_pct):
            return reject("min_atr_pct")

        # reject huge bars
        if self.cfg.max_bar_range_atr:
            bar_range = float(bar["high"]) - float(bar["low"])
            if (bar_range / atr) > float(self.cfg.max_bar_range_atr):
                return reject("max_bar_range_atr")

        # mean reversion regime: VWAP slope must be flat-ish
        slope = float(bar.get("vwap_slope", 0.0) or 0.0)
        if abs(slope) > float(self.cfg.slope_max):
            return reject("vwap_slope")

        # optional EMA regime gate
        ema_slope_max = float(getattr(self.cfg, "ema_slope_max", 0.0) or 0.0)
        if ema_slope_max > 0:
            # prefer normalized slope if available
            ema_slope_atr = bar.get("ema_slope_atr", None)
            if ema_slope_atr is not None:
                slope_val = float(ema_slope_atr or 0.0)
            else:
                slope_val = float(bar.get("ema_slope", 0.0) or 0.0)

            if abs(slope_val) > ema_slope_max:
                return reject("ema_slope_max")

        ema_max_dist = float(getattr(self.cfg, "ema_max_dist_atr", 0.0) or 0.0)
        if ema_max_dist > 0:
            ema_dist_atr = float(bar.get("ema_dist_atr", 0.0) or 0.0)
            if abs(ema_dist_atr) > ema_max_dist:
                return reject("ema_max_dist_atr")
            
        atr_pts = float(self.cfg.atr_mult) * atr
        if atr_pts <= 0:
            return reject("atr_pts<=0")

        # Read directional regime fields (safe getattr for backward compat)
        allow_longs  = bool(getattr(self.cfg, "allow_longs",  True))
        allow_shorts = bool(getattr(self.cfg, "allow_shorts", True))
        ema_slope_buy_min  = float(getattr(self.cfg, "ema_slope_buy_min",  -999.0) or -999.0)
        ema_slope_sell_max = float(getattr(self.cfg, "ema_slope_sell_max",  999.0) or  999.0)

        # Resolve ema_slope_atr for directional gates (same field used by ema_slope_max above)
        _ema_sa = bar.get("ema_slope_atr", None)
        _ema_slope_val: float = float(_ema_sa or 0.0) if _ema_sa is not None else 0.0

        side = None
        entry_price = px

        if px >= vwap + atr_pts:
            if not allow_shorts:
                return reject("shorts_disabled")
            if _ema_slope_val > ema_slope_sell_max:
                return reject("ema_slope_sell_max")
            side = "SELL"
            stop = entry_price + atr_pts + self.cfg.stop_pad_ticks * self.cfg.tick_size
            target = vwap if self.cfg.target_vwap else entry_price - self.cfg.target_R * abs(entry_price - stop)
            if self.cfg.target_vwap and float(getattr(self.cfg, "min_vwap_target_R", 0.0) or 0.0) > 0:
                stop_dist = abs(entry_price - stop)
                tgt_dist = abs(vwap - entry_price)
                if stop_dist <= 0 or (tgt_dist / stop_dist) < float(self.cfg.min_vwap_target_R):
                    return reject("min_vwap_target_R")

        elif px <= vwap - atr_pts:
            if not allow_longs:
                return reject("longs_disabled")
            if _ema_slope_val < ema_slope_buy_min:
                return reject("ema_slope_buy_min")
            side = "BUY"
            stop = entry_price - atr_pts - self.cfg.stop_pad_ticks * self.cfg.tick_size
            target = vwap if self.cfg.target_vwap else entry_price + self.cfg.target_R * abs(entry_price - stop)
            if self.cfg.target_vwap and float(getattr(self.cfg, "min_vwap_target_R", 0.0) or 0.0) > 0:
                stop_dist = abs(entry_price - stop)
                tgt_dist = abs(vwap - entry_price)
                if stop_dist <= 0 or (tgt_dist / stop_dist) < float(self.cfg.min_vwap_target_R):
                    return reject("min_vwap_target_R")

        else:
            return reject("no_setup")

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

        self._why["signal"] += 1

        return {"order": order, "bracket": bracket, "stop_dist_points": abs(entry_price - stop)}

    def score_signal(self, bar, decision) -> float:
        if not decision:
            return 0.0
        try:
            atr = float(bar.get("atr", 0.0) or 0.0)
            if atr <= 0:
                return 0.0
            entry_px = float(bar.get("close", 0.0) or 0.0)
            vwap = float(bar.get("vwap", entry_px) or entry_px)
            stop_dist = float(decision.get("stop_dist_points", 0.0) or 0.0)
            if stop_dist <= 0:
                return 0.0
            target_px = float(decision["bracket"].target_price)
            tgt_dist = abs(target_px - entry_px)
            rr = tgt_dist / stop_dist
            # Mean-reversion quality: more extended from VWAP = stronger setup
            dist_atr = abs(entry_px - vwap) / atr
            return float(rr + 0.5 * dist_atr)
        except Exception:
            return 0.0

