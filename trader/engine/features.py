import pandas as pd
import numpy as np
def add_session_vwap(df: pd.DataFrame, session_col: str | None = None) -> pd.DataFrame:
    df = df.copy()

    for col in ("high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Treat 0 volume as missing so it can't poison VWAP
    vol = df["volume"].replace(0.0, np.nan)

    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * vol

    if session_col and session_col in df.columns:
        num = pv.groupby(df[session_col]).cumsum()
        den = vol.groupby(df[session_col]).cumsum()
    else:
        num = pv.cumsum()
        den = vol.cumsum()

    df["vwap"] = (num / den).astype("float64")

    # Carry last known VWAP forward so later bars always have a usable vwap
    if session_col and session_col in df.columns:
        df["vwap"] = df["vwap"].groupby(df[session_col]).ffill()
    else:
        df["vwap"] = df["vwap"].ffill()

    df["vwap_slope"] = pd.to_numeric(df["vwap"].diff(), errors="coerce").fillna(0.0).astype("float64")
    return df
def add_atr(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    df = df.copy()
    high = pd.to_numeric(df["high"], errors="coerce")
    low  = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")

    prev_close = close.shift(1)

    tr = pd.concat(
        [(high - low).abs(),
         (high - prev_close).abs(),
         (low - prev_close).abs()],
        axis=1
    ).max(axis=1)

    # Wilder ATR
    df["atr"] = tr.ewm(alpha=1/length, adjust=False, min_periods=1).mean().astype("float64")
    return df
def add_opening_range(df: pd.DataFrame, minutes: int, session_key: str = "date") -> pd.DataFrame:
    if session_key not in df.columns:
        df[session_key] = df.index.date
    df["minutes_from_open"] = df.groupby(session_key).cumcount()
    mask = df["minutes_from_open"] < minutes
    agg = df[mask].groupby(session_key).agg(orh=("high", "max"), orl=("low", "min"))
    df = df.join(agg, on=session_key)
    return df
def add_rsi(df: pd.DataFrame, length: int = 14, col: str = "close") -> pd.DataFrame:
    df = df.copy()
    close = pd.to_numeric(df[col], errors="coerce").astype("float64")

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1/length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    df["rsi"] = pd.to_numeric(rsi, errors="coerce").fillna(50.0).astype("float64")
    df["rsi_prev"] = df["rsi"].shift(1).astype("float64")
    return df