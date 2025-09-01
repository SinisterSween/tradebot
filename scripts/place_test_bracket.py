# scripts/place_test_bracket.py
import asyncio, yaml, math
from ib_insync import util
from trader.brokers.ibkr import IbkrBroker

async def main():
    util.patchAsyncio()
    cfg = yaml.safe_load(open("config/settings.dev.yaml"))

    ibc = IbkrBroker(cfg["ibkr"]["host"], cfg["ibkr"]["port"], cfg["ibkr"]["client_id"], cfg["ibkr"]["account"])
    await ibc.connect()  # NOT readonly
    try:
        ibc.set_market_data_type(3 if cfg["ibkr"].get("use_delayed", False) else 1)

        # resolve + set contract
        c = await ibc.resolve_contract(
            cfg["symbol"], cfg["ibkr"]["exchange"], cfg["ibkr"]["currency"],
            cfg["ibkr"]["use_continuous"], cfg["ibkr"]["front_month"],
            con_id=cfg["ibkr"].get("con_id"), local_symbol=cfg["ibkr"].get("local_symbol")
        )
        ibc.contract = c

        tick = float(cfg["fees"]["tick_size"])
        last = 6500.00  # arbitrary anchor for a test order; we're going to cancel anyway
        side   = "BUY"
        limit  = round((last + 2*tick)/tick)*tick
        stop   = round((last -10*tick)/tick)*tick
        target = round((last +20*tick)/tick)*tick

        trades = await ibc.place_bracket_limit(side, 1, limit, stop, target, tif="GTC", outsideRth=True)
        ids = [t.order.orderId for t in trades]
        print("PLACED:", ids)

        await asyncio.sleep(3)  # let TWS register the orders

        # cancel children first, then parent
        for t in reversed(trades):
            try:
                ibc.ib.cancelOrder(t.order)
            except Exception as e:
                print(f"[CANCEL WARN] {e}")
        print("CANCELLED.")

    finally:
        await ibc.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
