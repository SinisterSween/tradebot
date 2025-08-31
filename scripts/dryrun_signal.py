# scripts/dryrun_signal.py
import asyncio, yaml, pandas as pd
from ib_insync import IB, util, Contract
from trader.engine.backtest import prepare_bars
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
from trader.engine.risk import RiskConfig, RiskGovernor

def to_utc_ts(d):
    """Robust UTC conversion for ib_insync BarData.date (str | datetime)."""
    if isinstance(d, str):
        d = util.parseIBDatetime(d)
    ts = pd.Timestamp(d)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")

async def main():
    util.patchAsyncio()
    cfg = yaml.safe_load(open("config/settings.dev.yaml"))

    ib = IB()
    await ib.connectAsync(cfg["ibkr"]["host"], cfg["ibkr"]["port"], clientId=cfg["ibkr"]["client_id"])
    try:
        ib.reqMarketDataType(3 if cfg["ibkr"].get("use_delayed", False) else 1)

        # Resolve exact contract via con_id (fast & unambiguous)
        con_id = int(cfg["ibkr"]["con_id"])
        cds = await ib.reqContractDetailsAsync(Contract(conId=con_id))
        if not cds:
            raise RuntimeError(f"No contract details for conId={con_id}")
        c = cds[0].contract

        # Pull last 1D of 1m bars
        bars = ib.reqHistoricalData(
            c, endDateTime="", durationStr="1 D", barSizeSetting="1 min",
            whatToShow=cfg["ibkr"]["what_to_show"], useRTH=False, formatDate=1, keepUpToDate=False
        )

        rows = [{
            "datetime": to_utc_ts(b.date),
            "open": float(b.open), "high": float(b.high),
            "low": float(b.low), "close": float(b.close),
            "volume": int(getattr(b, "volume", 0) or 0),
            "symbol": cfg["symbol"],
        } for b in bars]
        df = pd.DataFrame(rows)

        # Features
        fdf = prepare_bars(df, cfg["timezone"], cfg["strategy"]["orb_minutes"])

        # Strategy
        sc = cfg["strategy"]; fees = cfg["fees"]
        strat = HybridOrbVwap(StratConfig(
            orb_minutes=sc["orb_minutes"], atr_len=sc["atr_len"], break_eps_ticks=sc["break_eps_ticks"],
            atr_mult=sc["atr_mult"], target_R=sc["target_R"], slope_min=sc["slope_min"],
            stop_pad_ticks=sc["stop_pad_ticks"], trail_pad_ticks=sc["trail_pad_ticks"],
            tick_size=float(fees["tick_size"]), tick_value=float(fees["tick_value"]),
        ))
        windows = sc["session_windows"]

        # Real RiskGovernor so position_size etc. works
        rcfg = RiskConfig(
            account_equity=cfg["risk"]["account_equity"],
            risk_pct=cfg["risk"]["risk_pct"],
            max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
            max_consec_losses=cfg["risk"]["max_consec_losses"],
            tick_value=float(fees["tick_value"]),
            flat_time=cfg["risk"]["flat_time"],
            news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
        )
        risk = RiskGovernor(rcfg)

        # Walk recent bars and print first signal (if any)
        start_idx = max(10, len(fdf) - 60)
        for i in range(start_idx, len(fdf)):
            bar = fdf.iloc[i]
            sig = strat.maybe_signal(bar, windows, risk)
            if sig:
                print("SIGNAL:", sig)
                break
        else:
            print("No signal in the last ~60 bars (expected if conditions didn’t trigger).")

    finally:
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
