import asyncio, yaml
from ib_insync import IB, util

async def main():
    util.patchAsyncio()
    cfg = yaml.safe_load(open("config/settings.dev.yaml"))
    ib = IB()
    await ib.connectAsync(cfg["ibkr"]["host"], cfg["ibkr"]["port"], clientId=cfg["ibkr"]["client_id"])

    # reqMatchingSymbols is sync in ib_insync; don't await it
    matches = ib.reqMatchingSymbols("ES")
    futs = [m for m in matches if getattr(m.contract, "secType", "") == "FUT"]

    if not futs:
        print("No FUT results from reqMatchingSymbols('ES').")
    else:
        for m in futs:
            c = m.contract
            print(
                "match:",
                c.symbol, c.secType,
                c.exchange or "(no exch)",
                c.currency or "(no ccy)",
                "conId=", c.conId,
                "localSymbol=", getattr(c, "localSymbol", "")
            )

    ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
