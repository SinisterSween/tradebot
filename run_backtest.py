import yaml, pandas as pd
import matplotlib.pyplot as plt
import argparse
from trader.engine.backtest import prepare_bars, equity_curve
from trader.engine.execution import OrderManager
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
def main(cfg_path="config/settings.dev.yaml", show_gui=True):
    cfg = yaml.safe_load(open(cfg_path, "r"))
    df = pd.read_csv(cfg["data"]["csv_path"])
    df = prepare_bars(df, cfg["timezone"], cfg["strategy"]["orb_minutes"])
    fees = cfg["fees"]
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
    om = OrderManager(
        commission_per_contract=fees["commission_per_contract"],
        exchange_fees_per_contract=fees["exchange_fees_per_contract"],
        tick_size=fees["tick_size"],
    )
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
    for idx, bar in df.iterrows():
        if prev_date is None or bar["date"] != prev_date:
            risk.reset_day(); prev_date = bar["date"]; open_position = False; current_bracket = None
        if not risk.can_trade_now(bar["t_local"]):
            if open_position and current_bracket:
                bar2 = bar.copy(); bar2["high"] = bar["close"]; bar2["low"] = bar["close"]
                om.simulate_bracket(bar2, current_bracket)
            open_position = False; current_bracket = None; continue
        if open_position and current_bracket:
            exit_info = om.simulate_bracket(bar, current_bracket)
            if exit_info:
                R = (exit_info["exit_price"] - exit_info["entry_price"]) / (abs(exit_info["entry_price"] - current_bracket.stop_price))
                R *= (1 if exit_info["side"] == "BUY" else -1)
                risk.record_trade_outcome_R(R)
                open_position = False; current_bracket = None; continue
        if not open_position and not risk.halted:
            sig = strat.maybe_signal(bar, windows, risk)
            if sig:
                om.place_and_simulate(bar, sig["order"]); current_bracket = sig["bracket"]; open_position = True
    curve = equity_curve(om.trades)
    total_pnl = curve.iloc[-1] if not curve.empty else 0.0
    winners = [t for t in om.trades if t["action"] == "EXIT" and t["pnl"] > 0]
    losers = [t for t in om.trades if t["action"] == "EXIT" and t["pnl"] <= 0]
    hit_rate = (len(winners) / max(1, len(winners) + len(losers))) * 100.0
    print(f"Trades: {len(winners)+len(losers)}  |  Hit Rate: {hit_rate:.1f}%  |  Total PnL: ${total_pnl:,.2f}")
    if not curve.empty:
        print(f"Max Drawdown (approx): ${float((curve.cummax()-curve).max()):,.2f}")

    
    trades_df = pd.DataFrame(om.trades)
    trades_df.to_csv("logs/backtest_trades.csv", index=False)
    print("\nSaved trades to logs/backtest_trades.csv")
    print(trades_df.head(10))
    if not curve.empty:
        ax = curve.plot(title="Equity Curve")
        fig = ax.get_figure()
        fig.savefig("logs/equity_curve.png", dpi=120, bbox_inches="tight")
        print("Saved chart to logs/equity_curve.png")
        if show_gui:
            plt.show()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.dev.yaml")
    ap.add_argument("--no-gui", action="store_true", help="Save chart but do not show it")
    args = ap.parse_args()
    main(cfg_path=args.config, show_gui=not args.no_gui)
