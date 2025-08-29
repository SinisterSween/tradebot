import os, sys, pandas as pd
from trader.engine.metrics_ext import summarize_equity, write_artifacts

LOGS = "logs"
eq_path = os.path.join(LOGS, "equity_curve.csv")
trades_path = os.path.join(LOGS, "backtest_trades.csv")

equity = pd.read_csv(eq_path, index_col=0, parse_dates=True).iloc[:, 0] if os.path.exists(eq_path) else pd.Series(dtype=float)
trades = pd.read_csv(trades_path) if os.path.exists(trades_path) else None

summary = summarize_equity(equity, trades)
write_artifacts(equity, trades, summary, LOGS)
print("Wrote logs/summary.json")
