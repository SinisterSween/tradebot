# scripts/sanity_ib_delayed.py
import asyncio, yaml
from trader.brokers.ibkr import IbkrBroker

async def main(cfg_path="config/settings.dev.yaml"):
    cfg = yaml.safe_load(open(cfg_path, "r"))
    ibc = IbkrBroker(cfg["ibkr"]["host"], cfg["ibkr"]["port"], cfg["ibkr"]["client_id"], cfg["ibkr"]["account"])
    await ibc.connect()
    if cfg["ibkr"].get("use_delayed", False):
        ibc.set_market_data_type(3)  # delayed
    contract = await ibc.resolve_contract(cfg["symbol"], cfg["ibkr"]["exchange"], cfg["ibkr"]["currency"],
                                          cfg["ibkr"]["use_continuous"], cfg["ibkr"]["front_month"])

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

if __name__ == "__main__":
    asyncio.run(main())
