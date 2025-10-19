#!/usr/bin/env python3
import pandas as pd, datetime as dt, os, json

def main():
    os.makedirs("logs", exist_ok=True)
    today = dt.datetime.now().date()
    try:
        oos = pd.read_csv("logs/oos_log.csv", parse_dates=["run_ts"], infer_datetime_format=True)
        oos_today = oos[oos["run_ts"].dt.date == today]
    except Exception:
        oos_today = pd.DataFrame()

    try:
        tr = pd.read_csv("logs/backtest_trades.csv")
        exits = tr[tr.get("action","")=="EXIT"].copy()
    except Exception:
        exits = pd.DataFrame()

    parts = []
    parts.append(f"# OOS Daily Rollup — {today.isoformat()}")
    if not oos_today.empty:
        last = oos_today.iloc[-1].to_dict()
        parts.append(f"- Runs today: {len(oos_today)}")
        for k in ("Trades","WinRate","ProfitFactor","Expectancy","NetPnL","FeesTotal","MaxDrawdown"):
            if k in last:
                parts.append(f"- Last summary {k}: {last[k]}")
    else:
        parts.append("- No oos_log rows today.")

    if not exits.empty:
        g = exits.groupby("reason", dropna=False)["pnl"].agg(["count","mean","sum"]).sort_values("sum", ascending=False)
        parts.append("\n## Exit mix")
        parts.append(g.to_string())
        parts.append(f"\nTotal exits: {len(exits)} | PF={exits.loc[exits.pnl>0,'pnl'].sum()/max(1,-exits.loc[exits.pnl<=0,'pnl'].sum()):.2f} | Exp/exit={exits['pnl'].mean():.2f}")
    else:
        parts.append("\nNo exits available in latest trades file.")

    out_path = f"logs/daily_summary_{today.isoformat()}.txt"
    with open(out_path, "w") as f:
        f.write("\n".join(parts) + "\n")
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
