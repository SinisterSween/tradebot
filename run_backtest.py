import yaml, pandas as pd
import argparse
import os
from trader.engine.backtest import prepare_bars, equity_curve
from trader.engine.execution import OrderManager
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
from trader.engine.latency import BarDelay
from trader.engine.metrics_ext import summarize_equity, write_artifacts


def main(cfg_path="config/settings.dev.yaml", show_gui=True,
         latency_bars=0, fee_bps=1.0, fee_fixed=0.0, slip_bps=0.5,
         kill_dd=0.15, kill_day=0.05):
    import matplotlib
    if not show_gui:
        matplotlib.use("Agg")   # headless backend, no popups
    import matplotlib.pyplot as plt
    cfg = yaml.safe_load(open(cfg_path, "r"))
    df = pd.read_csv(cfg["data"]["csv_path"])
    df = prepare_bars(df, cfg["timezone"], cfg["strategy"]["orb_minutes"])
    fees = cfg["fees"]
    base_eq = cfg["risk"]["account_equity"]
    risk_cfg = RiskConfig(
        account_equity=cfg["risk"]["account_equity"],
        risk_pct=cfg["risk"]["risk_pct"],
        max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
        max_consec_losses=cfg["risk"]["max_consec_losses"],
        tick_value=fees["tick_value"],
        flat_time=cfg["risk"]["flat_time"],
        news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
    )
    risk = RiskGovernor(risk_cfg)
    dpp = fees["tick_value"] / fees["tick_size"]

    om = OrderManager(
        commission_per_contract=fees["commission_per_contract"],
        exchange_fees_per_contract=fees["exchange_fees_per_contract"],
        tick_size=fees["tick_size"],
        fee_bps=fee_bps if fee_bps is not None else fees.get("fee_bps", 1.0), 
        fee_fixed=fee_fixed if fee_fixed is not None else fees.get("fee_fixed", 0.0),
        slip_bps=slip_bps if slip_bps is not None else fees.get("slip_bps", 0.5),
        dollar_per_point=dpp,
    )
    lb = latency_bars if latency_bars is not None else cfg["fees"].get("latency_bars", 0)
    delay = BarDelay(bars=lb)

    sc = cfg["strategy"]
    strat = HybridOrbVwap(StratConfig(
        orb_minutes=sc["orb_minutes"],
        atr_len=sc["atr_len"],
        break_eps_ticks=sc["break_eps_ticks"],
        atr_mult=sc["atr_mult"],
        target_R=sc["target_R"],
        slope_min=sc["slope_min"],
        stop_pad_ticks=sc["stop_pad_ticks"],
        trail_pad_ticks=sc["trail_pad_ticks"],
        tick_size=fees["tick_size"],
        tick_value=fees["tick_value"],
    ))
    windows = sc["session_windows"]
    open_position = False
    current_bracket = None
    prev_date = None
    for i, (_, bar) in enumerate(df.iterrows()):
        if prev_date is None or bar["date"] != prev_date:
            risk.reset_day(base_eq + om.realized_pnl)
            prev_date = bar["date"] 
            open_position = False
            current_bracket = None

        for due_order in delay.due(i):
            om.place_and_simulate(bar, due_order)

        if not risk.can_trade_now(bar["t_local"]):
            if open_position and current_bracket:
                bar2 = bar.copy() 
                bar2["high"] = bar["close"] 
                bar2["low"] = bar["close"]
                om.simulate_bracket(bar2, current_bracket)
            open_position = False 
            current_bracket = None 
            continue

        if open_position and current_bracket:
            exit_info = om.simulate_bracket(bar, current_bracket)
            if exit_info:
                R = (exit_info["exit_price"] - exit_info["entry_price"]) / (abs(exit_info["entry_price"] - current_bracket.stop_price))
                R *= (1 if exit_info["side"] == "BUY" else -1)
                risk.record_trade_outcome_R(R)
                equity_now = base_eq + om.realized_pnl
                if risk.kill_tripped(equity_now):
                    risk.halted = True
                    open_position = False 
                    current_bracket = None 
                    continue

        if not open_position and not risk.halted:
            sig = strat.maybe_signal(bar, windows, risk)
            if sig:
                if delay.bars > 0:
                    delay.submit(i, sig["order"])
                else:
                    om.place_and_simulate(bar, sig["order"]) 
                current_bracket = sig["bracket"]
                open_position = True
            
    # --- Build equity + PnL series cleanly ---
    curve = equity_curve(om.trades)
    
    pnl_series = curve  # cumulative PnL from your engine
    equity_series = base_eq + pnl_series

    # Forward-fill equity onto the full bar timeline for proper daily returns
    if isinstance(equity_series.index, pd.DatetimeIndex) and isinstance(df.index, pd.DatetimeIndex):
        equity_full = equity_series.reindex(df.index, method="ffill").fillna(base_eq)
    else:
        equity_full = equity_series  # leave as-is; metrics_ext handles int index


    # Basic stats
    total_pnl = float(pnl_series.iloc[-1]) if not pnl_series.empty else 0.0
    winners = [t for t in om.trades if t["action"] == "EXIT" and t["pnl"] > 0]
    losers  = [t for t in om.trades if t["action"] == "EXIT" and t["pnl"] <= 0]
    hit_rate = (len(winners) / max(1, len(winners) + len(losers))) * 100.0
    print(f"Trades: {len(winners)+len(losers)}  |  Hit Rate: {hit_rate:.1f}%  |  Total PnL: ${total_pnl:,.2f}")

    # Save trades
    os.makedirs("logs", exist_ok=True)
    trades_df = pd.DataFrame(om.trades)
    trades_df.to_csv("logs/backtest_trades.csv", index=False)
    print("\nSaved trades to logs/backtest_trades.csv")
    print(trades_df.head(10))

    # Plot PnL so you can actually see movement
    if not pnl_series.empty:
        ax = pnl_series.plot(title="PnL (USD)")
        fig = ax.get_figure()
        fig.savefig("logs/pnl_curve.png", dpi=120, bbox_inches="tight")
        print("Saved chart to logs/pnl_curve.png")
        if show_gui:
            plt.show()
        plt.close(fig)
    if not equity_full.empty:
        ax2 = equity_full.plot(title="Equity (USD)")
        fig2 = ax2.get_figure()
        fig2.savefig("logs/equity_curve.png", dpi=120, bbox_inches="tight")
        if show_gui:
            plt.show()
        plt.close(fig2)


    # Metrics + artifacts based on full equity series
    summary = summarize_equity(equity_full, trades_df)
    write_artifacts(equity_full, trades_df, summary, logs_dir="logs")
    print("\n=== SUMMARY ===")
    print(f"Trades: {summary.get('Trades', 0)} | WinRate: {summary.get('WinRate', 0):.1f}% "
      f"| PF: {summary.get('ProfitFactor')} | Expectancy: ${summary.get('Expectancy', 0):.2f}")
    print(f"NetPnL: ${summary.get('NetPnL', 0):.2f} | Fees: ${summary.get('FeesTotal', 0):.2f} "
      f"| NetRet: {summary.get('NetReturnPct', 0):.2f}% | MDD: {summary.get('MaxDrawdown', 0):.2%}")

    print("Saved logs/summary.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.dev.yaml")
    ap.add_argument("--no-gui", action="store_true", help="Save chart but do not show it")
    ap.add_argument("--latency-bars", type=int, default=0, help="Bars of execution latency")
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--fee-fixed", type=float, default=0.0)
    ap.add_argument("--slip-bps", type=float, default=0.5)
    ap.add_argument("--kill-dd", type=float, default=0.15)
    ap.add_argument("--kill-day", type=float, default=0.05)
    args = ap.parse_args()
    main(cfg_path=args.config,
        show_gui=not args.no_gui,
        latency_bars=args.latency_bars,
        fee_bps=args.fee_bps,
        fee_fixed=args.fee_fixed,
        slip_bps=args.slip_bps,
        kill_dd=args.kill_dd,
        kill_day=args.kill_day)
