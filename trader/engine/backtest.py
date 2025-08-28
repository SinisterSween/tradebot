import pandas as pd
from .features import add_session_vwap, add_atr, add_opening_range
def prepare_bars(df: pd.DataFrame, tz: str, orb_minutes: int) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df["datetime"], utc=True).tz_convert(tz)
    df["date"] = df.index.date
    df["t_local"] = df.index.strftime("%H:%M")
    df["symbol"] = df.get("symbol", "ES")
    df = add_session_vwap(df, session_col="date")
    df = add_atr(df, length=14)
    df = add_opening_range(df, minutes=orb_minutes, session_key="date")
    return df
def equity_curve(trades) -> pd.Series:
    pnl = [t.get("pnl", 0.0) - t.get("fees", 0.0) if t["action"] == "EXIT" else -t.get("fees", 0.0) for t in trades]
    return pd.Series(pnl).cumsum()
