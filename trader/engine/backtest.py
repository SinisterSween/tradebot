import pandas as pd
from .features import add_session_vwap, add_atr, add_opening_range, add_rsi
import numpy as np
def equity_curve(trades) -> pd.Series:
    pnl = []
    for t in trades:
        if t.get("action") == "EXIT":
            pnl.append(float(t.get("pnl", 0.0) or 0.0) - float(t.get("fees_total", 0.0) or 0.0))
        else:
            pnl.append(-float(t.get("fees_total", 0.0) or 0.0))
    return pd.Series(pnl).cumsum()


def prepare_bars(
    df: pd.DataFrame,
    tz: str,
    orb_minutes: int,
    ema_fast_len: int = 50,
    ema_slope_lookback: int = 5,
) -> pd.DataFrame:
    df = df.copy()

    # --- 1) Resolve timestamp column ---
    ts_col = None
    for c in ["datetime", "ts", "t_utc", "timestamp", "date_time", "time"]:
        if c in df.columns:
            ts_col = c
            break
    if ts_col is None:
        raise ValueError(f"prepare_bars: no timestamp column found. Have={list(df.columns)}")

    df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
    df = df.dropna(subset=[ts_col])

    # --- 2) Normalize OHLCV column names ---
    colmap = {}

    def pick(*candidates):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    o = pick("open", "Open", "o", "O")
    h = pick("high", "High", "h", "H")
    l = pick("low",  "Low",  "l", "L")
    c = pick("close","Close","c", "C", "price", "Price", "last", "Last")
    v = pick("volume","Volume","v", "V", "vol", "Vol")

    if o: colmap[o] = "open"
    if h: colmap[h] = "high"
    if l: colmap[l] = "low"
    if c: colmap[c] = "close"
    if v: colmap[v] = "volume"

    df = df.rename(columns=colmap)

    # Require at least close; ORB/ATR also want high/low.
    missing = [x for x in ["close", "high", "low"] if x not in df.columns]
    if missing:
        raise ValueError(f"prepare_bars: missing required columns {missing}. Have={list(df.columns)}")

    # Ensure numeric
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "high", "low"])

    # --- 3) Set DatetimeIndex and localize ---
    df = df.sort_values(ts_col).reset_index(drop=True)
    df.set_index(ts_col, inplace=True)
    df.index = df.index.tz_convert(tz)

    # --- 4) Derived fields ---
    df["date"] = df.index.date
    df["t_local"] = df.index.strftime("%H:%M")
    if "symbol" not in df.columns:
        df["symbol"] = "ES"

    # --- 5) Features ---
    df = add_session_vwap(df, session_col="date")
    # RSI (for RsiMeanReversion)
    rsi_len = int(14)
    if "rsi_len" in df.columns:
        # ignore; you won't have this as a column. keep fixed.
        pass

    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / rsi_len, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_len, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    df["rsi"] = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0).astype("float64")

    if "vwap_slope" not in df.columns:
        df["vwap_slope"] = df["vwap"].diff().fillna(0.0)
        
    df["rsi_prev"] = df["rsi"].shift(1)

    # ATR must exist before we can normalize EMA slope / dist in ATR units
    df = add_atr(df, length=14)

    # Opening range
    df = add_opening_range(df, minutes=orb_minutes, session_key="date")
    
    # --- EMA features (single source of truth) ---
    ema_fast_len = int(ema_fast_len or 50)
    ema_slope_lookback = int(ema_slope_lookback or 5)

    # EMA on close
    df["ema_fast"] = df["close"].ewm(span=ema_fast_len, adjust=False).mean()

    # EMA slope over lookback (difference vs N bars ago)
    df["ema_slope"] = df["ema_fast"].diff(ema_slope_lookback)
    df["ema_slope"] = pd.to_numeric(df["ema_slope"], errors="coerce").fillna(0.0).astype("float64")

    # Distance from EMA in ATR units
    atr_safe = pd.to_numeric(df["atr"], errors="coerce").replace(0.0, np.nan).astype("float64")
    df["ema_dist_atr"] = ((df["close"] - df["ema_fast"]).abs() / atr_safe)
    df["ema_dist_atr"] = pd.to_numeric(df["ema_dist_atr"], errors="coerce").fillna(0.0).astype("float64")

    # Rolling max deviation from EMA in ATR units (use EMA length as window)
    dev = ((df["close"] - df["ema_fast"]).abs() / atr_safe)
    df["ema_max_dev_atr"] = dev.rolling(ema_fast_len, min_periods=1).max()
    df["ema_max_dev_atr"] = pd.to_numeric(df["ema_max_dev_atr"], errors="coerce").fillna(0.0).astype("float64")

    return df
