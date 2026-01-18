# trader/strategies/rsi_mean_reversion.py
from dataclasses import dataclass
from ..engine.utils import Order, Bracket
import math
from collections import Counter
@dataclass
class StratConfig:
    rsi_len: int = 14
    rsi_buy_below: float = 28.0
    rsi_sell_above: float = 72.0

    atr_mult: float = 1.0
    target_R: float = 0.8
    stop_pad_ticks: int = 2
    allow_longs: bool = True
    allow_shorts: bool = False

    # Optional regime/vol gates
    slope_max: float = 0.0          # only trade if abs(vwap_slope) <= slope_max (if > 0)
    min_atr_pct: float = 0.0        # only trade if atr/close >= min_atr_pct (if > 0)

    tick_size: float = 0.01
    tick_value: float = 1.0

    # Target behavior
    target_vwap: bool = True       # if True, aim for VWAP instead of R target
    vwap_dist_atr_min: float = 0.0
    # ---- NEW: throttling + signal quality knobs ----
    require_cross: bool = True          # only trigger on threshold cross, not "still below"
    min_minutes_between_trades: int = 30
    max_trades_per_day: int = 6
    ema_slope_max: float = 0.0
    ema_slope_atr_min: float = -0.2
    ema_dist_atr_min: float = 0.0    
    ema_dist_atr_max: float = 0.0
    exit_on_ema: bool = False
    exit_ema_len: int = 20
    exit_ema_side: str = "cross"
    min_vwap_target_R: float = 0.0
    vwap_entry_min_atr: float = 0.0
    vwap_entry_max_atr: float = 0.0
    use_session_open_stop: bool = False
    session_open_stop_atr_pad: float = 0.0
    use_early_range_target: bool = False
    early_range_mult: float = 1.0
    early_min_range_atr: float = 0.0
    require_early_done: bool = True


class RsiMeanReversion:
    def __init__(self, cfg: StratConfig):
        self.cfg = cfg
        self._last_trade_ts = None          # pd.Timestamp
        self._day = None                    # datetime.date
        self._trades_today = 0
        self._why = Counter()
        self._rej = Counter()
        print(f"[RsiMeanReversion] target_vwap={self.cfg.target_vwap}")

    def on_entry_submitted(self, ts) -> None:
        self._trades_today += 1
        self._last_trade_ts = ts



    def window_ok(self, ts_local_str: str, windows) -> bool:
        return any(w["start"] <= ts_local_str <= w["end"] for w in windows)

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

    def maybe_signal(self, bar, windows, risk):
        def reject(reason: str):
            self._why[reason] += 1
            return None
        if not self.window_ok(bar["t_local"], windows):
            return reject("window")

        self._reset_if_new_day(bar)

        close = float(bar.get("close", 0.0) or 0.0)
        atr = float(bar.get("atr", 0.0) or 0.0)
        vwap_raw = bar.get("vwap", close)
        try:
            vwap = float(vwap_raw)
        except Exception:
            vwap = close

        if not math.isfinite(close) or not math.isfinite(atr):
            return None
        if (not math.isfinite(vwap)) or vwap <= 0:
            vwap = close

        if close <= 0 or atr <= 0:
            return reject("bad_price_or_atr")

        # min atr% gate
        if self.cfg.min_atr_pct and (atr / close) < float(self.cfg.min_atr_pct):
            return reject("min_atr_pct")
        
        
        vwap_dist_atr = bar.get("dist_from_vwap_atr", None)
        if vwap_dist_atr is None:
            return reject("no_dist_from_vwap_atr")
        try:
            vwap_dist_atr = float(vwap_dist_atr)
        except Exception:
            return reject("bad_dist_from_vwap_atr")

        min_vwap_dist = float(getattr(self.cfg, "vwap_dist_atr_min", 0.0) or 0.0)
        if min_vwap_dist > 0 and vwap_dist_atr < min_vwap_dist:
            return reject("vwap_dist_atr_min")
        

        # vwap slope regime gate (optional)
        if float(self.cfg.slope_max or 0.0) > 0:
            slope = bar.get("vwap_slope", 0.0)
            try:
                slope = float(slope)
            except Exception:
                slope = 0.0
            if not math.isfinite(slope):
                slope = 0.0
            if abs(slope) > float(self.cfg.slope_max):
                return reject("vwap_slope")

        # Throttle: max trades/day
        if int(self.cfg.max_trades_per_day or 0) > 0 and self._trades_today >= int(self.cfg.max_trades_per_day):
            return reject("max_trades_per_day")

        # Throttle: min minutes between entries
        ts_now = bar.name if hasattr(bar, "name") else None
        if ts_now is not None:
            min_m = int(self.cfg.min_minutes_between_trades or 0)
            if min_m > 0 and self._minutes_since_last(ts_now) < min_m:
                return reject("min_minutes_between_trades")

        
        # RSI values - require prepare_bars to provide rsi and (optionally) rsi_prev
        rsi = bar.get("rsi", None)
        if rsi is None:
            return reject("no_rsi")
        rsi = float(rsi)
        if not math.isfinite(rsi):
            return reject("bad_rsi")

        rsi_prev = bar.get("rsi_prev", None)
        rsi_prev = float(rsi_prev) if rsi_prev is not None else None
        if rsi_prev is not None and not math.isfinite(rsi_prev):
            rsi_prev = None

        # --- EMA slope regime gate (ATR-normalized) ---
        ema_slope_atr = bar.get("ema_slope_atr", None)
        if ema_slope_atr is None:
            return reject("no_ema_slope_atr")
        try:
            ema_slope_atr = float(ema_slope_atr)
        except Exception:
            return reject("bad_ema_slope_atr")
        if not math.isfinite(ema_slope_atr):
            return reject("bad_ema_slope_atr")
        
        allow_longs  = bool(getattr(self.cfg, "allow_longs", True))
        allow_shorts = bool(getattr(self.cfg, "allow_shorts", False))

        if allow_longs:
            if ema_slope_atr < float(getattr(self.cfg, "ema_slope_atr_min", -0.2)):
                return reject("ema_slope_atr_min")

        if float(getattr(self.cfg, "ema_slope_max", 0.0) or 0.0) > 0:
            if abs(ema_slope_atr) > float(self.cfg.ema_slope_max):
                return reject("ema_slope_max")

        ema_dist_atr = bar.get("ema_dist_atr", None)
        if ema_dist_atr is None:
            return reject("no_ema_dist_atr")
        try:
            ema_dist_atr = float(ema_dist_atr)
        except Exception:
            return reject("bad_ema_dist_atr")
        if not math.isfinite(ema_dist_atr):
            return reject("bad_ema_dist_atr")
        
        if float(getattr(self.cfg, "ema_dist_atr_min", 0.0) or 0.0) > 0:
            if ema_dist_atr < float(self.cfg.ema_dist_atr_min):
                return reject("ema_dist_atr_min")
        if float(getattr(self.cfg, "ema_dist_atr_max", 0.0) or 0.0) > 0:
            if ema_dist_atr > float(self.cfg.ema_dist_atr_max):
                return reject("ema_dist_atr_max")

        

        require_cross = bool(getattr(self.cfg, "require_cross", True))
        session_open = float(bar.get("session_open", close) or close)
        early_range = float(bar.get("early_range", 0.0) or 0.0)
        early_done = int(bar.get("early_done", 0) or 0)

        if bool(getattr(self.cfg, "require_early_done", False)) and early_done == 0:
            return reject("early_not_done")

        early_min_range_atr = float(getattr(self.cfg, "early_min_range_atr", 0.0) or 0.0)
        if early_min_range_atr > 0 and atr > 0:
            if int(bar.get("early_done", 0) or 0) == 1:
                if (early_range / atr) < early_min_range_atr:
                    return reject("early_range_too_small")

        buy_th = float(self.cfg.rsi_buy_below)
        sell_th = float(self.cfg.rsi_sell_above)
        allow_shorts = bool(getattr(self.cfg, "allow_shorts", False))
        allow_longs = bool(getattr(self.cfg, "allow_longs", True))
        side = None
        if require_cross and rsi_prev is not None:
            # Cross DOWN through buy_th => long
            if allow_longs and (rsi_prev > buy_th and rsi <= buy_th):
                side = "BUY"
            # Cross UP through sell_th => short
            elif allow_shorts and (rsi_prev < sell_th and rsi >= sell_th):
                side = "SELL"
            else:
                return reject("no_cross")
        else:
            # Non-cross mode (will overtrade)
            if allow_longs and rsi <= buy_th:
                side = "BUY"
            elif allow_shorts and rsi >= sell_th:
                side = "SELL"
            else:
                return reject("no_threshold")

        # Direction gates
        if side == "BUY" and not bool(getattr(self.cfg, "allow_longs", True)):
            return reject("longs_disabled")
        if side == "SELL" and not bool(getattr(self.cfg, "allow_shorts", True)):
            return reject("shorts_disabled")


        entry_price = close
        atr_pts = float(self.cfg.atr_mult) * atr
        if atr_pts <= 0:
            return reject("bad_price_or_atr")
        if side == "BUY":
            stop = entry_price - atr_pts - self.cfg.stop_pad_ticks * self.cfg.tick_size
            if self.cfg.target_vwap:
                target = vwap if vwap > entry_price else (
                    entry_price + float(self.cfg.target_R) * abs(entry_price - stop)
                    )
            else:
                stop_dist = abs(entry_price - stop)
                target = entry_price + float(self.cfg.target_R) * stop_dist

            if bool(getattr(self.cfg, "use_session_open_stop", False)):
                pad = float(getattr(self.cfg, "session_open_stop_atr_pad", 0.0) or 0.0)
                stop = min(stop, session_open - pad * atr)

            if bool(getattr(self.cfg, "use_early_range_target", False)):
                mult = float(getattr(self.cfg, "early_range_mult", 1.0) or 1.0)
                measured = session_open + (early_range * mult)
                
                if measured > entry_price:
                    target = measured

            vwap_dist_atr = abs(entry_price - vwap) / atr if atr > 0 else 0.0
            
            if self.cfg.vwap_entry_min_atr and vwap_dist_atr < float(self.cfg.vwap_entry_min_atr):
                return reject("vwap_entry_min_atr")
            if self.cfg.vwap_entry_max_atr and vwap_dist_atr > float(self.cfg.vwap_entry_max_atr):
                return reject("vwap_entry_max_atr")
            
            if self.cfg.target_vwap and float(getattr(self.cfg, "min_vwap_target_R", 0.0) or 0.0) > 0:
                stop_dist = abs(entry_price - stop)
                if stop_dist <= 0:
                    return None
                r_to_vwap = abs(entry_price - vwap) / stop_dist
                if r_to_vwap < float(self.cfg.min_vwap_target_R):
                    return reject("min_vwap_target_R")
        else:  # SELL
            stop = entry_price + atr_pts + self.cfg.stop_pad_ticks * self.cfg.tick_size

            if self.cfg.target_vwap:
                target = vwap if vwap < entry_price else (
                    entry_price - float(self.cfg.target_R) * abs(entry_price - stop)
                    )
            else:
                stop_dist = abs(entry_price - stop)
                target = entry_price - float(self.cfg.target_R) * stop_dist

            if bool(getattr(self.cfg, "use_session_open_stop", False)):
                pad = float(getattr(self.cfg, "session_open_stop_atr_pad", 0.0) or 0.0)
                stop = max(stop, session_open + pad * atr)

            if bool(getattr(self.cfg, "use_early_range_target", False)):
                mult = float(getattr(self.cfg, "early_range_mult", 1.0) or 1.0)
                measured = session_open - (early_range * mult)
                # only use it if it's actually below entry
                if measured < entry_price:
                    target = measured

            vwap_dist_atr = abs(entry_price - vwap) / atr if atr > 0 else 0.0

            if self.cfg.vwap_entry_min_atr and vwap_dist_atr < float(self.cfg.vwap_entry_min_atr):
                return reject("vwap_entry_min_atr")
            if self.cfg.vwap_entry_max_atr and vwap_dist_atr > float(self.cfg.vwap_entry_max_atr):
                return reject("vwap_entry_max_atr")

            if self.cfg.target_vwap and float(getattr(self.cfg, "min_vwap_target_R", 0.0) or 0.0) > 0:
                stop_dist = abs(entry_price - stop)
                if stop_dist <= 0:
                    return reject("bad_stop_dist")
                r_to_vwap = abs(entry_price - vwap) / stop_dist
                if r_to_vwap < float(self.cfg.min_vwap_target_R):
                    return reject("min_vwap_target_R")
                
        stop_dist_pts = abs(entry_price - stop)

        order_id = str(bar.get("ts") or bar.get("datetime") or bar.name)
        order = Order(
            id=order_id,
            ts=bar.name,
            symbol=bar["symbol"],
            side=side,
            qty=1,              # runner will size this (crypto/equity/futures)
            type="MARKET",
        )
        bracket = Bracket(stop_price=stop, target_price=target)

        return {"order": order, "bracket": bracket, "stop_dist_points": stop_dist_pts}