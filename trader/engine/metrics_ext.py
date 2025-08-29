import os, json
import numpy as np
import pandas as pd

def _daily_from_equity(equity: pd.Series) -> pd.Series:
    if isinstance(equity.index, pd.DatetimeIndex):
        return equity.resample("1D").last().dropna()
    if len(equity) == 0:
        return equity
    bars_per_day = 390  # approx minute bars in RTH
    grp = np.arange(len(equity)) // bars_per_day
    return equity.groupby(grp).last()

def summarize_equity(equity: pd.Series, trades: pd.DataFrame | None) -> dict:
    if equity is None or len(equity) == 0:
        return {}

    equity = equity.astype(float).sort_index()
    daily = _daily_from_equity(equity)
    rets = daily.pct_change().dropna()

    if isinstance(equity.index, pd.DatetimeIndex):
        span_days = max(1, (equity.index[-1] - equity.index[0]).days or 1)
        years = span_days / 365.0
    else:
        years = max(1e-9, len(daily) / 252.0)

    if len(rets):
        log_growth = np.log1p(rets).sum()
        cagr = float(np.exp(log_growth / years) - 1.0)
    else:
        cagr = 0.0

    sharpe = float((rets.mean() / (rets.std() + 1e-12)) * np.sqrt(252)) if len(rets) else 0.0
    neg = rets[rets < 0]
    sortino = float((rets.mean() / (neg.std() + 1e-12)) * np.sqrt(252)) if len(neg) else 0.0

    if len(daily):
        roll_max = daily.cummax()
        max_dd = float((1.0 - (daily / roll_max)).max())
    else:
        max_dd = 0.0

    # ----- Trade metrics (EXIT-only for PnL stats, ENTRY-only for turnover) -----
    trades_n = 0
    win_rate = 0.0
    profit_factor = None
    turnover = 0.0
    avg_trade = 0.0
    net_pnl = 0.0
    fees_total = 0.0
    gross_pnl = 0.0
    net_ret_pct = 0.0
    gross_ret_pct = 0.0
    avg_win = 0.0
    avg_loss = 0.0
    payoff_ratio = None
    expectancy = avg_trade

    start_eq = float(equity.iloc[0]) if len(equity) else 1.0

    if trades is not None and len(trades):
        # Exit rows drive PnL-based stats (already net of fees in your pipeline)
        exits = trades[trades.get("action", "") == "EXIT"] if "action" in trades.columns else trades
        trades_n = int(len(exits))
        

        if trades_n and "pnl" in exits.columns:
            wins_mask = exits["pnl"] > 0
            losses_mask = exits["pnl"] < 0

            n_wins = int(wins_mask.sum())
            n_losses = int(losses_mask.sum())

            if n_wins:
                avg_win = float(exits.loc[wins_mask, "pnl"].mean())
            if n_losses:
                avg_loss = float(exits.loc[losses_mask, "pnl"].mean())  # negative

            wins = n_wins
            win_rate = float(wins / trades_n) * 100.0
            gp = float(exits.loc[wins_mask, "pnl"].sum())
            gl = float(-exits.loc[losses_mask, "pnl"].sum())
            profit_factor = float(gp / gl) if gl > 0 else None

            avg_trade = float(exits["pnl"].mean())
            net_pnl = float(exits["pnl"].sum())

            if n_losses and abs(avg_loss) > 1e-12:
                payoff_ratio = float(avg_win / abs(avg_loss))

            # Expectancy via p*avg_win + (1-p)*avg_loss (should match avg_trade)
            p = (n_wins / trades_n) if trades_n else 0.0
            expectancy = float(p * avg_win + (1.0 - p) * avg_loss)

            if "fees_total" in exits.columns:
                fees_total = float(exits["fees_total"].sum())
            elif "fees" in exits.columns:
                fees_total = float(exits["fees"].sum())
            else:
                fees_total = 0.0

            gross_pnl = float(net_pnl + fees_total)

            if start_eq > 0:
                net_ret_pct = float((net_pnl / start_eq) * 100.0)
                gross_ret_pct = float((gross_pnl / start_eq) * 100.0)

        # Turnover from ENTRY notional only, normalized by starting equity
        if "notional" in trades.columns:
            entries = trades[trades.get("action", "") == "ENTRY"] if "action" in trades.columns else trades
            turnover_notional = float(entries["notional"].abs().sum())
            turnover = float(turnover_notional / max(1e-12, start_eq))
        
    return {
        "CAGR": cagr,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "MaxDrawdown": max_dd,
        "WinRate": win_rate,
        "ProfitFactor": profit_factor,
        "Turnover": turnover,
        "AvgTrade": avg_trade,
        "Trades": trades_n,
        "NetPnL": net_pnl,
        "GrossPnL": gross_pnl,
        "FeesTotal": fees_total,
        "NetReturnPct": net_ret_pct,
        "GrossReturnPct": gross_ret_pct,
        "Expectancy": expectancy,
        "AvgWin": avg_win,
        "AvgLoss": avg_loss,
        "PayoffRatio": payoff_ratio,
    }

def write_artifacts(equity: pd.Series, trades: pd.DataFrame | None, summary: dict, logs_dir: str = "logs") -> None:
    os.makedirs(logs_dir, exist_ok=True)
    if isinstance(equity, pd.Series):
        equity.to_csv(os.path.join(logs_dir, "equity_curve.csv"))
    if trades is not None:
        trades.to_csv(os.path.join(logs_dir, "backtest_trades.csv"), index=False)
    with open(os.path.join(logs_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
