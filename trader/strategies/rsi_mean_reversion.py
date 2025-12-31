# trader/strategies/rsi_mean_reversion.py
from dataclasses import dataclass
from ..engine.utils import Order, Bracket
import pandas as pd

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
    target_vwap: bool = False       # if True, aim for VWAP instead of R target

    # ---- NEW: throttling + signal quality knobs ----
    require_cross: bool = True          # only trigger on threshold cross, not "still below"
    min_minutes_between_trades: int = 30
    max_trades_per_day: int = 6
    ema_slope_max: float = 0.0       # if > 0, require abs(ema_slope) <= this
    ema_dist_atr_min: float = 0.0    # if > 0, require ema_dist_atr >= this
    ema_dist_atr_max: float = 0.0    # if > 0, require ema_dist_atr <= this



class RsiMeanReversion:
    def __init__(self, cfg: StratConfig):
        self.cfg = cfg
        self._last_trade_ts = None          # pd.Timestamp
        self._day = None                    # datetime.date
        self._trades_today = 0
        print(f"[RsiMeanReversion] target_vwap={self.cfg.target_vwap}")

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
        if not self.window_ok(bar["t_local"], windows):
            return None

        self._reset_if_new_day(bar)

        close = float(bar.get("close", 0.0) or 0.0)
        atr = float(bar.get("atr", 0.0) or 0.0)
        vwap = float(bar.get("vwap", close) or close)

        if close <= 0 or atr <= 0:
            return None

        # min atr% gate
        if self.cfg.min_atr_pct and (atr / close) < float(self.cfg.min_atr_pct):
            return None

        # vwap slope regime gate (optional)
        if float(self.cfg.slope_max or 0.0) > 0:
            slope = float(bar.get("vwap_slope", 0.0) or 0.0)
            if abs(slope) > float(self.cfg.slope_max):
                return None

        # Throttle: max trades/day
        if int(self.cfg.max_trades_per_day or 0) > 0 and self._trades_today >= int(self.cfg.max_trades_per_day):
            return None

        # Throttle: min minutes between entries
        ts_now = bar.name if hasattr(bar, "name") else None
        if ts_now is not None:
            min_m = int(self.cfg.min_minutes_between_trades or 0)
            if min_m > 0 and self._minutes_since_last(ts_now) < min_m:
                return None

        # RSI values - require prepare_bars to provide rsi and (optionally) rsi_prev
        rsi = bar.get("rsi", None)
        if rsi is None:
            return None
        rsi = float(rsi)

        rsi_prev = bar.get("rsi_prev", None)
        rsi_prev = float(rsi_prev) if rsi_prev is not None else None

        # --- EMA regime / stretch gates (optional) ---
        if float(getattr(self.cfg, "ema_slope_max", 0.0) or 0.0) > 0:
            ema_slope = float(bar.get("ema_slope", 0.0) or 0.0)
            if abs(ema_slope) > float(self.cfg.ema_slope_max):
                return None

        ema_dist_atr = float(bar.get("ema_dist_atr", 0.0) or 0.0)
        if float(getattr(self.cfg, "ema_dist_atr_min", 0.0) or 0.0) > 0:
            if ema_dist_atr < float(self.cfg.ema_dist_atr_min):
                return None
        if float(getattr(self.cfg, "ema_dist_atr_max", 0.0) or 0.0) > 0:
            if ema_dist_atr > float(self.cfg.ema_dist_atr_max):
                return None

        buy_th = float(self.cfg.rsi_buy_below)
        sell_th = float(self.cfg.rsi_sell_above)

        # Decide whether we require a cross
        require_cross = bool(getattr(self.cfg, "require_cross", True))

        if require_cross and rsi_prev is not None:
            buy_cross  = (rsi_prev > buy_th and rsi <= buy_th)
            sell_cross = (rsi_prev < sell_th and rsi >= sell_th)
        else:
            buy_cross  = (rsi <= buy_th)
            sell_cross = (rsi >= sell_th)

        # Determine signal from crosses
        if buy_cross:
            side = "BUY"
        elif sell_cross:
            side = "SELL"
        else:
            return None

        rsi = float(rsi)

        rsi_prev = bar.get("rsi_prev", None)
        if rsi_prev is not None:
            rsi_prev = float(rsi_prev)

        # Entry thresholds
        buy_th = float(self.cfg.rsi_buy_below)
        sell_th = float(self.cfg.rsi_sell_above)

        # Determine signal (prefer crosses)
        side = None
        if bool(self.cfg.require_cross) and rsi_prev is not None:
            # Cross DOWN through buy_th => long
            if rsi_prev > buy_th and rsi <= buy_th:
                side = "BUY"
            # Cross UP through sell_th => short
            elif rsi_prev < sell_th and rsi >= sell_th:
                side = "SELL"
            else:
                return None
        else:
            # Legacy behavior (will overtrade)
            if rsi <= buy_th:
                side = "BUY"
            elif rsi >= sell_th:
                side = "SELL"
            else:
                return None
        # ---- Direction gate ----
        if side == "BUY" and not bool(getattr(self.cfg, "allow_longs", True)):
            return None
        if side == "SELL" and not bool(getattr(self.cfg, "allow_shorts", True)):
            return None

        entry_price = close
        atr_pts = float(self.cfg.atr_mult) * atr
        if atr_pts <= 0:
            return None

        if side == "BUY":
            stop = entry_price - atr_pts - self.cfg.stop_pad_ticks * self.cfg.tick_size
            if self.cfg.target_vwap:
                target = vwap
            else:
                stop_dist = abs(entry_price - stop)
                target = entry_price + float(self.cfg.target_R) * stop_dist
        else:  # SELL
            stop = entry_price + atr_pts + self.cfg.stop_pad_ticks * self.cfg.tick_size
            if self.cfg.target_vwap:
                target = vwap
            else:
                stop_dist = abs(entry_price - stop)
                target = entry_price - float(self.cfg.target_R) * stop_dist

        stop_dist_pts = abs(entry_price - stop)
        qty = risk.position_size(stop_dist_pts, self.cfg.tick_size)
        if qty <= 0:
            return None

        order_id = str(bar.get("ts") or bar.get("datetime") or bar.name)
        order = Order(
            id=order_id,
            ts=bar.name,
            symbol=bar["symbol"],
            side=side,
            qty=qty,
            type="MARKET",
        )
        bracket = Bracket(stop_price=stop, target_price=target)

        # Record the entry for throttles
        self._trades_today += 1
        if ts_now is not None:
            self._last_trade_ts = ts_now

        return {"order": order, "bracket": bracket, "stop_dist_points": stop_dist_pts}
