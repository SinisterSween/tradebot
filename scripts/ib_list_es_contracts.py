# scripts/ib_list_es_contracts.py
import asyncio, yaml
from ib_insync import IB, util, Future

async def main():
    util.patchAsyncio()
    cfg = yaml.safe_load(open("config/settings.dev.yaml"))
    ib = IB()
    await ib.connectAsync(cfg["ibkr"]["host"], cfg["ibkr"]["port"], clientId=cfg["ibkr"]["client_id"])

    # Probe CME first, then GLOBEX
    contracts = []
    for exch in ("CME", "GLOBEX", ""):
        cds = await ib.reqContractDetailsAsync(Future(symbol="ES", exchange=exch or None, currency="USD"))
        if cds:
            for cd in cds:
                c = cd.contract
                contracts.append((c.localSymbol, c.exchange, c.lastTradeDateOrContractMonth, c.conId))
            break

    if not contracts:
        print("No ES futures returned from reqContractDetails.")
    else:
        for ls, ex, ltm, cid in contracts[:25]:  # show first 25
            print(f"ES -> {ls:10s} exch={ex or '(none)'} month={ltm} conId={cid}")

    ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
