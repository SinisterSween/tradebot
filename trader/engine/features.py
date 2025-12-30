import pandas as pd
def add_session_vwap(df: pd.DataFrame, session_col: str = None) -> pd.DataFrame:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    if session_col and session_col in df.columns:
        df["vwap"] = (pv.groupby(df[session_col]).cumsum()
                      / df["volume"].groupby(df[session_col]).cumsum())
    else:
        df["vwap"] = pv.cumsum() / df["volume"].cumsum()
    df["vwap_slope"] = df["vwap"].diff().fillna(0.0)
    return df
def add_atr(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(length, min_periods=1).mean()
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
    close = pd.to_numeric(df[col], errors="coerce")

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    # Wilder-style smoothing via EMA with alpha=1/length
    avg_gain = gain.ewm(alpha=1/length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    df["rsi"] = pd.to_numeric(rsi, errors="coerce").fillna(50.0).astype(float)
    return df