import asyncio, yaml, math
from ib_insync import IB, util, Contract
from trader.brokers.ibkr import IbkrBroker

async def main():
    util.patchAsyncio()
    cfg = yaml.safe_load(open("config/settings.dev.yaml"))
    ibc = IbkrBroker(cfg["ibkr"]["host"], cfg["ibkr"]["port"], cfg["ibkr"]["client_id"], cfg["ibkr"]["account"])
    await ibc.connect(readonly=True)
    try:
        ibc.set_market_data_type(3 if cfg["ibkr"].get("use_delayed", False) else 1)
        c = await ibc.resolve_contract(
            cfg["symbol"], cfg["ibkr"]["exchange"], cfg["ibkr"]["currency"],
            cfg["ibkr"]["use_continuous"], cfg["ibkr"]["front_month"],
            con_id=cfg["ibkr"].get("con_id"), local_symbol=cfg["ibkr"].get("local_symbol")
        )
        ibc.contract = c
        tick = float(cfg["fees"]["tick_size"])
        last = 6500.00  # dummy ref; we’re just validating structure/commissions

        side   = "BUY"
        limit  = round((last + 2*tick) / tick) * tick
        stop   = round((last - 10*tick) / tick) * tick
        target = round((last + 20*tick) / tick) * tick

        out = await ibc.whatif_bracket_limit(side, qty=1, limit_price=limit, stop=stop, target=target)
        print("WHATIF PARENT:", out["parent"])
        print("WHATIF TAKE  :", out["take"])
        print("WHATIF STOP  :", out["stop"])
    finally:
        await ibc.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
