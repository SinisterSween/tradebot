# test_ib.py
from ib_insync import IB, util, Future

HOST = "127.0.0.1"   # must match TWS Global Config → API → Settings → 'Socket Port' host
PORT = 7497          # 7497 = TWS Paper, 7496 = TWS Live
CLIENT_ID = 999      # any int; unique per TWS session

ib = IB()
try:
    print(f"Connecting to {HOST}:{PORT} clientId={CLIENT_ID} ...")
    ib.connect(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15)
    print("Connected:", ib.isConnected())
    print("Server version:", ib.client.serverVersion())
    print("TWS time:", ib.reqCurrentTime())

    # Optional: show accounts and positions (non-blocking if none)
    accts = ib.managedAccounts()
    print("Accounts:", accts)
    pos = ib.positions()
    print("Positions:", [ (p.account, getattr(p.contract, 'symbol', None), p.position) for p in pos ])

    # Optional: resolve a futures contract (continuous or front month)
    # Example: ES (E-mini S&P 500) front month on CME
    es = Future(symbol='ES', lastTradeDateOrContractMonth='202509', exchange='CME', currency='USD')
    es = ib.qualifyContracts(es)[0]
    print("ES contract conId:", es.conId, "localSymbol:", es.localSymbol)

    print("✅ Basic API handshake looks good.")
except Exception as e:
    print("❌ Failed to connect:", repr(e))
    print("Hints:")
    print(" - Start TWS desktop and log in (Paper or Live).")
    print(" - Edit → Global Configuration → API → Settings:")
    print("     • Enable ActiveX and Socket Clients")
    print("     • Socket Port must match this script (7497 paper / 7496 live)")
    print("     • Allow connections from localhost (or add 127.0.0.1 to Trusted IPs)")
    print(" - If another app is using the same clientId, pick a different CLIENT_ID.")
finally:
    try:
        ib.disconnect()
    except Exception:
        pass
