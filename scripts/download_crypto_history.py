"""
Download historical 1m OHLCV data from Binance.US for a given symbol
and save it to data/<SYM>_USDT_live_1m.csv in the format the backtest expects.

Usage:
    python scripts/download_crypto_history.py ADA/USDT
    python scripts/download_crypto_history.py BTC/USDT --days 180
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import ccxt.pro as ccxt


async def download(symbol: str, days: int = 180, out_dir: str = "data"):
    api_key = os.getenv("BINANCEUS_API_KEY", "")
    secret  = os.getenv("BINANCEUS_SECRET", "")

    exchange = ccxt.binanceus({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })

    try:
        await exchange.load_markets()

        since_ms = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
        print(f"Downloading {symbol} — {days} days of 1m bars from Binance.US...")

        all_bars = []
        batch_since = since_ms
        limit = 1000

        while True:
            batch = await exchange.fetch_ohlcv(symbol, timeframe="1m", since=batch_since, limit=limit)
            if not batch:
                break
            all_bars.extend(batch)
            last_ts = batch[-1][0]
            print(f"  fetched {len(all_bars)} bars so far (last: {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).isoformat()})")
            if len(batch) < limit:
                break
            batch_since = last_ts + 60_000  # next minute
            await asyncio.sleep(0.5)  # rate limit

        print(f"Total bars downloaded: {len(all_bars)}")

        # Write CSV
        os.makedirs(out_dir, exist_ok=True)
        base = symbol.replace("/", "_")
        out_path = os.path.join(out_dir, f"{base}_live_1m.csv")

        with open(out_path, "w") as f:
            f.write("datetime,open,high,low,close,volume\n")
            for ts, o, h, l, c, v in all_bars:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                f.write(f"{dt},{o},{h},{l},{c},{v}\n")

        print(f"Saved to {out_path}")

    finally:
        await exchange.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", help='e.g. ADA/USDT')
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()
    asyncio.run(download(args.symbol, days=args.days, out_dir=args.out_dir))
