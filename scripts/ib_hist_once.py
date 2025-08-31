import asyncio, yaml
from ib_insync import IB, util, Contract

async def main():
    util.patchAsyncio()

    cfg = yaml.safe_load(open("config/settings.dev.yaml"))
    host = cfg["ibkr"]["host"]; port = cfg["ibkr"]["port"]; cid = cfg["ibkr"]["client_id"]
    con_id = cfg["ibkr"].get("con_id")
    if not con_id:
        raise SystemExit("Set ibkr.con_id in YAML first (e.g., ESU5 = 637533641).")

    ib = IB()

    err_log = []
    def on_err(reqId, code, msg, _json=''):
        if len(err_log) < 20:
            print(f"[IB-ERR] {code}: {msg}")
            err_log.append((code, msg))
    ib.errorEvent += on_err

    try:
        print(f"[STEP] connect {host}:{port} cid={cid}")
        await ib.connectAsync(host, port, clientId=cid, timeout=10)
        print(f"[OK] connected? {ib.isConnected()}")

        print("[STEP] set marketDataType=3 (DELAYED)")
        ib.reqMarketDataType(3)

        print(f"[STEP] qualify by conId={con_id}")
        cds = await ib.reqContractDetailsAsync(Contract(conId=int(con_id)))
        if not cds:
            print("[FAIL] no contract details for conId, abort.")
            return
        c = cds[0].contract
        print(f"[OK] qualified: localSymbol={c.localSymbol} exch={c.exchange} month={c.lastTradeDateOrContractMonth} conId={c.conId}")

        print("[STEP] request historical (1 D, 1 min, TRADES, keepUpToDate=False)")
        bars = ib.reqHistoricalData(
            c,
            endDateTime='',
            durationStr='1 D',
            barSizeSetting='1 min',
            whatToShow='TRADES',
            useRTH=False,
            formatDate=1,
            keepUpToDate=False
        )

        # give TWS a tick; do NOT await ib.sleep(...)
        await asyncio.sleep(0.5)

        print(f"[RESULT] bars received: {len(bars)}")
        for b in list(bars)[-3:]:
            print(f"BAR {b.date} O={b.open} H={b.high} L={b.low} C={b.close} V={getattr(b, 'volume', 0)}")

    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
