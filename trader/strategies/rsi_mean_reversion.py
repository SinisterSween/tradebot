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

    # Optional regime/vol gates
    slope_max: float = 0.0          # only trade if abs(vwap_slope) <= slope_max (if > 0)
    min_atr_pct: float = 0.0        # only trade if atr/close >= min_atr_pct (if > 0)

    tick_size: float = 0.01
    tick_value: float = 1.0

    # Optional behavior knobs
    target_vwap: bool = False       # if True, aim for VWAP instead of R target


class RsiMeanReversion:
    def __init__(self, cfg: StratConfig):
        self.cfg = cfg

    def window_ok(self, ts_local_str: str, windows) -> bool:
        return any(w["start"] <= ts_local_str <= w["end"] for w in windows)
    
    def _rsi(self, series: pd.Series, length: int) -> pd.Series:
        # Wilder RSI
        delta = series.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)

        avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0.0, pd.NA)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi.fillna(50.0)

    def maybe_signal(self, bar, windows, risk):
        if not self.window_ok(bar["t_local"], windows):
            return None

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

        # We need RSI value available. Expect prepare_bars to compute it.
        rsi = bar.get("rsi", None)
        if rsi is None:
            return None
        rsi = float(rsi)

        # Mean reversion triggers
        side = None
        entry_price = close
        atr_pts = float(self.cfg.atr_mult) * atr
        if atr_pts <= 0:
            return None

        if rsi <= float(self.cfg.rsi_buy_below):
            side = "BUY"
            stop = entry_price - atr_pts - self.cfg.stop_pad_ticks * self.cfg.tick_size
            if self.cfg.target_vwap:
                target = vwap
            else:
                stop_dist = abs(entry_price - stop)
                target = entry_price + float(self.cfg.target_R) * stop_dist

        elif rsi >= float(self.cfg.rsi_sell_above):
            side = "SELL"
            stop = entry_price + atr_pts + self.cfg.stop_pad_ticks * self.cfg.tick_size
            if self.cfg.target_vwap:
                target = vwap
            else:
                stop_dist = abs(entry_price - stop)
                target = entry_price - float(self.cfg.target_R) * stop_dist

        else:
            return None

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
        return {"order": order, "bracket": bracket, "stop_dist_points": stop_dist_pts}