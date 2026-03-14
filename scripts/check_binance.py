#!/usr/bin/env python3
"""
Quick diagnostic: show open orders and coin balances on Binance.US.
Run with:  python scripts/check_binance.py
"""
import asyncio, os
from dotenv import load_dotenv

load_dotenv()

SYMBOLS = ["ETH/USDT", "SOL/USDT", "XRP/USDT", "LTC/USDT"]
MIN_USD  = 1.0   # ignore dust below this

async def main():
    import ccxt.pro as ccxt
    exchange = ccxt.binanceus({
        'apiKey': os.environ.get("BINANCEUS_API_KEY", ""),
        'secret': os.environ.get("BINANCEUS_SECRET", ""),
        'enableRateLimit': True,
    })

    try:
        await exchange.load_markets()

        # --- balances ---
        bal   = await exchange.fetch_balance()
        usdt  = float((bal.get("USDT") or {}).get("free", 0))
        print(f"\n{'='*50}")
        print(f"  USDT free: ${usdt:.2f}")
        print(f"{'='*50}")

        for sym in SYMBOLS:
            base  = sym.split("/")[0]
            total = float((bal.get(base) or {}).get("total", 0))
            if total > 0:
                ticker = await exchange.fetch_ticker(sym)
                usd_val = total * float(ticker["last"])
                if usd_val >= MIN_USD:
                    print(f"  {base}: {total:.6f}  (~${usd_val:.2f})")

        # --- open orders ---
        print(f"\n{'='*50}")
        print("  OPEN ORDERS")
        print(f"{'='*50}")
        any_orders = False
        for sym in SYMBOLS:
            orders = await exchange.fetch_open_orders(sym)
            if orders:
                any_orders = True
                for o in orders:
                    print(f"  {sym}  id={o['id']}  type={o['type']}  "
                          f"side={o['side']}  amount={o['amount']}  "
                          f"price={o.get('price')}  stopPrice={o.get('info', {}).get('stopPrice')}")
        if not any_orders:
            print("  (none)")

        print(f"\n{'='*50}\n")

    finally:
        await exchange.close()

asyncio.run(main())
