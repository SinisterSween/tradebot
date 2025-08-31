# scripts/sanity_ib_delayed.py
import asyncio, yaml
from trader.brokers.ibkr import IbkrBroker
from ib_insync import util
import argparse

async def main(cfg_path="config/settings.dev.yaml"):
    util.patchAsyncio()
    cfg = yaml.safe_load(open(cfg_path, "r"))
    ibc = IbkrBroker(cfg["ibkr"]["host"], cfg["ibkr"]["port"], cfg["ibkr"]["client_id"], cfg["ibkr"]["account"])
    try:
        await ibc.connect()
        if cfg["ibkr"].get("use_delayed", False):
            ibc.set_market_data_type(3)  # delayed
        contract = await ibc.resolve_contract(
            cfg["symbol"],
            cfg["ibkr"]["exchange"],
            cfg["ibkr"]["currency"],
            cfg["ibkr"]["use_continuous"],
            cfg["ibkr"]["front_month"],
            con_id=cfg["ibkr"].get("con_id"),
            local_symbol=cfg["ibkr"].get("local_symbol"),
        )
        ibc.contract = contract

        setattr(ibc, "contract", contract)
        seen = 0
        async def on_bar(b):
            nonlocal seen
            print("BAR", b.get("datetime"), b.get("close"))
            seen += 1
            if seen >= 3:
                await ibc.disconnect()

        # IMPORTANT: this should internally use reqHistoricalData(..., keepUpToDate=True) when delayed
        await ibc.stream_realtime_bars(on_bar=on_bar,
                                    what_to_show=cfg["ibkr"]["what_to_show"],
                                    bar_size_secs=cfg["ibkr"]["bar_size_secs"])
        # keep process alive until disconnect
        while seen < 3:
            await asyncio.sleep(0.5)
    finally:
        try:
            await ibc.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.dev.yaml")
    args = ap.parse_args()
    asyncio.run(main(cfg_path=args.config))
