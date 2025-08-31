# scripts/scan_signals.py
import asyncio, yaml, pandas as pd
from ib_insync import IB, util, Contract
from trader.engine.backtest import prepare_bars
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
from trader.engine.risk import RiskConfig, RiskGovernor

def to_utc_ts(d):
    if isinstance(d, str):
        d = util.parseIBDatetime(d)
    ts = pd.Timestamp(d)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")

def pick_bar_time(row):
    for k in ("t_local", "t_utc", "datetime"):
        if k in row.index:
            return row[k]
    return row.name

async def main(days=5, cooldown_bars=5):
    util.patchAsyncio()
    cfg = yaml.safe_load(open("config/settings.dev.yaml"))

    ib = IB()
    await ib.connectAsync(cfg["ibkr"]["host"], cfg["ibkr"]["port"], clientId=cfg["ibkr"]["client_id"])
    try:
        ib.reqMarketDataType(3 if cfg["ibkr"].get("use_delayed", False) else 1)
        con_id = int(cfg["ibkr"]["con_id"])
        cds = await ib.reqContractDetailsAsync(Contract(conId=con_id))
        if not cds: raise RuntimeError(f"No contract details for conId={con_id}")
        c = cds[0].contract

        bars = ib.reqHistoricalData(
            c, endDateTime="", durationStr=f"{int(days)} D", barSizeSetting="1 min",
            whatToShow=cfg["ibkr"]["what_to_show"], useRTH=False, formatDate=1, keepUpToDate=False
        )
        df = pd.DataFrame([{
            "datetime": to_utc_ts(b.date),
            "open": float(b.open), "high": float(b.high),
            "low": float(b.low), "close": float(b.close),
            "volume": int(getattr(b, "volume", 0) or 0),
            "symbol": cfg["symbol"],
        } for b in bars])

        fdf = prepare_bars(df, cfg["timezone"], cfg["strategy"]["orb_minutes"])

        sc, fees = cfg["strategy"], cfg["fees"]
        strat = HybridOrbVwap(StratConfig(
            orb_minutes=sc["orb_minutes"], atr_len=sc["atr_len"], break_eps_ticks=sc["break_eps_ticks"],
            atr_mult=sc["atr_mult"], target_R=sc["target_R"], slope_min=sc["slope_min"],
            stop_pad_ticks=sc["stop_pad_ticks"], trail_pad_ticks=sc["trail_pad_ticks"],
            tick_size=float(fees["tick_size"]), tick_value=float(fees["tick_value"]),
        ))
        windows = sc["session_windows"]
        risk = RiskGovernor(RiskConfig(
            account_equity=cfg["risk"]["account_equity"], risk_pct=cfg["risk"]["risk_pct"],
            max_daily_loss_R=cfg["risk"]["max_daily_loss_R"], max_consec_losses=cfg["risk"]["max_consec_losses"],
            tick_value=float(fees["tick_value"]), flat_time=cfg["risk"]["flat_time"],
            news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
        ))

        hits = []
        prev_date = None
        last_sig_idx = -10_000
        for i in range(len(fdf)):
            bar = fdf.iloc[i]

            # daily reset like run_live
            if prev_date is None or bar.get("date") != prev_date:
                risk.reset_day(); prev_date = bar.get("date")

            # session gate
            tloc = bar.get("t_local", bar.get("datetime"))
            if not risk.can_trade_now(tloc):
                continue

            # cooldown
            if i - last_sig_idx < cooldown_bars:
                continue

            sig = strat.maybe_signal(bar, windows, risk)
            if sig:
                hits.append({
                    "day": str(bar.get("date")),         # <— add this
                    "t": str(tloc),
                    "side": sig["order"].side,
                    "qty": sig["order"].qty,
                    "close": float(bar.get("close", float("nan"))),
                    "stop": float(sig["bracket"].stop_price),
                    "target": float(sig["bracket"].target_price),
                })
                last_sig_idx = i

        # Summaries
        print(f"Signals found over ~{days}d with gating+cooldown({cooldown_bars} bars): {len(hits)}")
        by_day = {}
        for h in hits:
            by_day[h["day"]] = by_day.get(h["day"], 0) + 1
        print("Per-day counts:", by_day)
        for h in hits[-10:]:
            print(h)

       

    finally:
        ib.disconnect()

if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    asyncio.run(main(days))
