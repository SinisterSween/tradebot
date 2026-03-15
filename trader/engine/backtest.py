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
    rsi_len: int = 14,
    atr_len: int = 14,
    symbol: str | None = None,
    exit_on_ema: bool = False,
    exit_ema_len: int = 20,
    verbose: bool = True,
) -> pd.DataFrame:
    df = df.copy()

    if verbose:
        print(f"[PREP] tz={tz} orb={orb_minutes} rsi_len={rsi_len} atr_len={atr_len} ema_fast_len={ema_fast_len}")
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
    df["symbol"] = symbol or "UNKNOWN"

    def add_session_open_and_early_range(
        df: pd.DataFrame,
        session_col: str = "date",
        early_minutes: int = 60,
    ) -> pd.DataFrame:
        df = df.copy()
        # Ensure session_col exists
        if session_col not in df.columns:
            df[session_col] = df.index.date

        idx = df.index
        if hasattr(idx, "tz") and idx.tz is not None:
            idx_naive = idx.tz_convert("UTC").tz_localize(None)
        else:
            idx_naive = pd.DatetimeIndex(idx)
        
        session_start = pd.Series(idx_naive, index=df.index).groupby(df[session_col]).transform("min")
        minutes_from_start = (pd.Series(idx_naive, index=df.index) - session_start).dt.total_seconds() / 60.0
        # session_open = first close of the day (you can use open if your feed has it consistently)
        df["session_open"] = df.groupby(session_col)["close"].transform("first")

        # build a per-session minute index from the first bar timestamp
        # session_start = df.groupby(session_col).apply(lambda g: g.index.min())
        # session_start = session_start.reindex(df[session_col]).to_numpy()
        #minutes_from_start = ((df.index.to_numpy() - session_start) / np.timedelta64(1, "m")).astype("float64")
        df["minutes_from_session_start"] = minutes_from_start.astype("float64")

        # mask for early window (based on t_local which is already tz-converted)
        in_early = (df["minutes_from_session_start"] >= 0) & (df["minutes_from_session_start"] < float(early_minutes))
        df["in_early_window"] = in_early.astype(int)

        # early_high/low computed per-day using only bars in the early window
        early_high = df["high"].where(in_early).groupby(df[session_col]).transform("max")
        early_low  = df["low"].where(in_early).groupby(df[session_col]).transform("min")

        # forward-fill so later bars can see the early window result
        df["early_high"] = early_high.groupby(df[session_col]).ffill()
        df["early_low"]  = early_low.groupby(df[session_col]).ffill()

        df["early_range"] = (df["early_high"] - df["early_low"]).astype("float64")
        #df["early_move_from_open"] = (df["close"] - df["session_open"]).astype("float64")

        # whether we've reached end of early window yet (so early_range is "final")
        df["early_done"] = (df["minutes_from_session_start"] >= float(early_minutes)).astype(int)

        return df

    df= add_session_open_and_early_range(
        df,
        session_col="date",
        early_minutes=60,
    )

    # bars: df has minutes_from_start
    if int(orb_minutes or 0) > 0:
        day_start = df.index.normalize()  # midnight UTC
        df["minutes_from_start"] = ((df.index - day_start).total_seconds() / 60.0).astype(int)
        df["orb_ok"] = df["minutes_from_start"] >= int(orb_minutes)
    else:
        df["orb_ok"] = True


    # --- 5) Features ---
    df = add_session_vwap(df, session_col="date")


    # RSI (for RsiMeanReversion)
    rsi_len = int(rsi_len or 14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / rsi_len, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_len, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    df["rsi"] = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0).astype("float64")
    df["rsi_prev"] = df["rsi"].shift(1)

    if "vwap_slope" not in df.columns:
        df["vwap_slope"] = df["vwap"].diff().fillna(0.0)

    
        
    # ATR must exist before we can normalize EMA slope / dist in ATR units
    atr_len = int(atr_len or 14)
    df = add_atr(df, length=int(atr_len or 14))

    # dist from vwap in ATR units (needs ATR)
    atr_safe = pd.to_numeric(df["atr"], errors="coerce").replace(0.0, np.nan).astype("float64")
    df["dist_from_vwap_atr"] = ((df["close"] - df["vwap"]).abs() / atr_safe).replace([np.inf, -np.inf], np.nan)
    df["dist_from_vwap_atr"] = pd.to_numeric(df["dist_from_vwap_atr"], errors="coerce").fillna(0.0).astype("float64")
    

    if verbose:
        print("[SANITY.FEAT] vwap nan%:", float(df["vwap"].isna().mean()))
        print("[SANITY.FEAT] atr  nan%:", float(df["atr"].isna().mean()))
        print("[SANITY.FEAT] atr<=0 %:", float((df["atr"] <= 0).mean()))
        print("[SANITY.FEAT] volume<=0 %:", float((df["volume"] <= 0).mean()) if "volume" in df.columns else -1)

    # Opening range
    df = add_opening_range(df, minutes=orb_minutes, session_key="date")
    
    # --- EMA features (single source of truth) ---
    ema_fast_len = int(ema_fast_len or 50)
    ema_slope_lookback = int(ema_slope_lookback or 5)

    

    df["ema_fast"] = df["close"].ewm(span=ema_fast_len, adjust=False).mean()
    df[f"ema_exit_{exit_ema_len}"] = df["close"].ewm(span=exit_ema_len, adjust=False).mean()
    df["ema_200"]  = df["close"].ewm(span=200, adjust=False).mean()   # long-term regime filter
    df["ema"] = df["ema_fast"]
    df["ema_slope"] = df["ema_fast"].diff(ema_slope_lookback)
    df["ema_slope"] = pd.to_numeric(df["ema_slope"], errors="coerce").fillna(0.0).astype("float64")
    
    atr_safe = pd.to_numeric(df["atr"], errors="coerce").astype("float64")
    atr_safe = atr_safe.where(atr_safe > 0)  # <=0 -> NaN

    ema_slope_atr = (df["ema_slope"] / atr_safe).replace([np.inf, -np.inf], np.nan)
    df["ema_slope_atr"] = pd.to_numeric(ema_slope_atr, errors="coerce")  # keep NaN

    ema_dist_atr = ((df["close"] - df["ema_fast"]).abs() / atr_safe).replace([np.inf, -np.inf], np.nan)
    df["ema_dist_atr"] = pd.to_numeric(ema_dist_atr, errors="coerce")  # keep NaN

    dev = ((df["close"] - df["ema_fast"]).abs() / atr_safe).replace([np.inf, -np.inf], np.nan)
    df["ema_max_dev_atr"] = pd.to_numeric(
        dev.rolling(ema_fast_len, min_periods=1).max(),
        errors="coerce"
    )  # keep NaN

    if verbose:
        print(f"[FEATURES] rsi_len={rsi_len} ready")

    return df
