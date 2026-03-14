#!/usr/bin/env python3
"""
Show recent Binance.US order history (last 20 filled/cancelled orders per symbol).
Run with:  python scripts/show_history.py
"""
import asyncio, os
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

SYMBOLS = ["ETH/USDT", "SOL/USDT", "XRP/USDT", "LTC/USDT"]
LIMIT   = 20  # orders per symbol

def fmt_ts(ts_ms):
    if not ts_ms:
        return "?"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M:%S")

async def main():
    import ccxt.pro as ccxt
    exchange = ccxt.binanceus({
        'apiKey': os.environ.get("BINANCEUS_API_KEY", ""),
        'secret': os.environ.get("BINANCEUS_SECRET", ""),
        'enableRateLimit': True,
    })

    try:
        await exchange.load_markets()

        for sym in SYMBOLS:
            print(f"\n{'='*60}")
            print(f"  {sym} — last {LIMIT} orders")
            print(f"{'='*60}")
            print(f"  {'TIME(UTC)':<17} {'TYPE':<18} {'SIDE':<5} {'AMT':>10} {'PRICE':>10} {'STATUS':<10} {'ID'}")
            print(f"  {'-'*100}")

            try:
                orders = await exchange.fetch_orders(sym, limit=LIMIT)
                # sort newest first
                orders = sorted(orders, key=lambda o: o.get('timestamp') or 0, reverse=True)
                if not orders:
                    print("  (no orders found)")
                for o in orders:
                    ts       = fmt_ts(o.get('timestamp'))
                    otype    = o.get('type', '?')
                    side     = o.get('side', '?')
                    amount   = o.get('amount') or 0
                    price    = o.get('price') or o.get('info', {}).get('stopPrice') or 0
                    status   = o.get('status', '?')
                    oid      = o.get('id', '?')
                    filled   = o.get('filled') or 0
                    cost     = o.get('cost') or (filled * float(price) if price else 0)
                    # highlight status
                    flag = ""
                    if status == 'canceled':  flag = " ← OCO cancel"
                    if status == 'closed':    flag = " ✓ FILLED"
                    print(f"  {ts:<17} {otype:<18} {side:<5} {amount:>10.4f} {float(price):>10.4f} "
                          f"{status:<10} {oid}{flag}  cost≈${cost:.2f}")
            except Exception as e:
                print(f"  ERROR: {e}")

        print(f"\n{'='*60}\n")

    finally:
        await exchange.close()

asyncio.run(main())
