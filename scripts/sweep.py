import yaml, itertools, pandas as pd
from trader.engine.backtest import prepare_bars, equity_curve
from trader.engine.execution import OrderManager
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig

def run_once(cfg):
    import pandas as pd
    df = pd.read_csv(cfg["data"]["csv_path"])
    df = prepare_bars(df, cfg["timezone"], cfg["strategy"]["orb_minutes"])

    fees = cfg["fees"]
    risk = RiskGovernor(RiskConfig(
        account_equity=cfg["risk"]["account_equity"],
        risk_pct=cfg["risk"]["risk_pct"],
        max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
        max_consec_losses=cfg["risk"]["max_consec_losses"],
        tick_value=fees["tick_value"],
        flat_time=cfg["risk"]["flat_time"],
        news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
    ))
    om = OrderManager(
        commission_per_contract=fees["commission_per_contract"],
        exchange_fees_per_contract=fees["exchange_fees_per_contract"],
        tick_size=fees["tick_size"],
    )
    sc = cfg["strategy"]
    strat = HybridOrbVwap(StratConfig(
        orb_minutes=sc["orb_minutes"], atr_len=sc["atr_len"], break_eps_ticks=sc["break_eps_ticks"],
        atr_mult=sc["atr_mult"], target_R=sc["target_R"], slope_min=sc["slope_min"],
        stop_pad_ticks=sc["stop_pad_ticks"], trail_pad_ticks=sc["trail_pad_ticks"],
        tick_size=fees["tick_size"], tick_value=fees["tick_value"],
    ))
    windows = sc["session_windows"]

    open_position = False; current_bracket = None; prev_date = None
    for _, bar in df.iterrows():
        if prev_date is None or bar["date"] != prev_date:
            risk.reset_day(); prev_date = bar["date"]; open_position=False; current_bracket=None
        if not risk.can_trade_now(bar["t_local"]):
            if open_position and current_bracket:
                bar2 = bar.copy(); bar2["high"]=bar["close"]; bar2["low"]=bar["close"]
                om.simulate_bracket(bar2, current_bracket)
            open_position=False; current_bracket=None; continue
        if open_position and current_bracket:
            exit_info = om.simulate_bracket(bar, current_bracket)
            if exit_info:
                R = (exit_info["exit_price"] - exit_info["entry_price"]) / (abs(exit_info["entry_price"] - current_bracket.stop_price))
                R *= (1 if exit_info["side"] == "BUY" else -1)
                risk.record_trade_outcome_R(R)
                open_position=False; current_bracket=None; continue
        if not open_position and not risk.halted:
            sig = strat.maybe_signal(bar, windows, risk)
            if sig:
                om.place_and_simulate(bar, sig["order"]); current_bracket=sig["bracket"]; open_position=True

    curve = equity_curve(om.trades)
    total_pnl = float(curve.iloc[-1]) if not curve.empty else 0.0
    dd = float((curve.cummax()-curve).max()) if not curve.empty else 0.0
    wins = [t for t in om.trades if t["action"]=="EXIT" and t["pnl"]>0]
    losses = [t for t in om.trades if t["action"]=="EXIT" and t["pnl"]<=0]
    trades = len(wins)+len(losses)
    hit = (len(wins)/max(1,trades))*100.0
    pf = (sum(t["pnl"] for t in wins) / max(1e-9, -sum(t["pnl"] for t in losses))) if losses else float('inf')
    return {"trades":trades, "hit_rate":hit, "pnl":total_pnl, "max_dd":dd, "pf":pf}

def main():
    base = yaml.safe_load(open("config/settings.dev.yaml"))
    sc = base["strategy"]

    grid = {
        "break_eps_ticks": [0,1,2],
        "atr_mult": [0.5,0.8,1.0,1.2],
        "target_R": [1.0,1.2,1.4],
    }

    rows=[]
    keys=list(grid.keys())
    for vals in itertools.product(*[grid[k] for k in keys]):
        cfg = yaml.safe_load(open("config/settings.dev.yaml"))
        for k,v in zip(keys, vals):
            cfg["strategy"][k]=v
        m = run_once(cfg)
        rows.append({**{k:v for k,v in zip(keys,vals)}, **m})

    df = pd.DataFrame(rows).sort_values(["pnl","pf","hit_rate"], ascending=[False,False,False])
    df.to_csv("logs/sweep_results.csv", index=False)
    print(df.head(15).to_string(index=False))

if __name__ == "__main__":
    main()
