"""
Crypto pair scanner for Binance.US.

Fetches all active USDT pairs, filters by minimum 24h volume,
computes normalised ATR and trend metrics from recent 1m bars,
and returns a ranked list of the best candidates for trading.

Usage (standalone):
    python -m trader.engine.crypto_scanner

Usage (from run_live.py):
    from trader.engine.crypto_scanner import scan_top_symbols
    symbols = await scan_top_symbols(exchange, top_n=5)
"""

import asyncio
from typing import List, Optional


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------

async def scan_top_symbols(
    exchange,
    *,
    top_n: int = 5,
    min_volume_usd: float = 100_000,   # 24h volume floor in USD
    min_atr_pct: float = 0.003,        # ATR must be >= 0.3% of price
    lookback_bars: int = 200,          # 1m bars to pull per symbol
    quote: str = "USDT",
    prefer_trending: bool = True,      # rank by trend strength; False = rank by raw volatility
    verbose: bool = True,
) -> List[str]:
    """
    Returns up to top_n USDT pair symbols ranked by trading quality.

    Scoring (prefer_trending=True):
        score = atr_pct * (1 + abs(ema_slope_atr))
        Prefers coins that are both volatile AND trending — ideal for TrendPullback.

    Scoring (prefer_trending=False):
        score = atr_pct
        Prefers most volatile coins regardless of direction — ideal for mean reversion.
    """
    await exchange.load_markets()

    # 1. All active USDT spot markets
    candidates = [
        s for s, m in exchange.markets.items()
        if m.get("quote") == quote
        and m.get("active", True)
        and "/" in s
        and not m.get("type", "spot").startswith("swap")
    ]
    if verbose:
        print(f"[SCANNER] {len(candidates)} active {quote} pairs found")

    # 2. Filter by 24h volume
    try:
        tickers = await exchange.fetch_tickers(candidates)
    except Exception as e:
        print(f"[SCANNER] fetch_tickers error: {e} — falling back to individual fetches")
        tickers = {}
        for sym in candidates:
            try:
                tickers[sym] = await exchange.fetch_ticker(sym)
            except Exception:
                pass

    liquid = []
    for sym in candidates:
        t = tickers.get(sym, {})
        vol = float(t.get("quoteVolume") or t.get("baseVolume", 0) or 0)
        last = float(t.get("last") or 0)
        # quoteVolume is already in USDT; baseVolume * last converts base to USDT
        if "quoteVolume" not in t or t["quoteVolume"] is None:
            vol = vol * last  # baseVolume → USD
        if vol >= min_volume_usd and last > 0:
            liquid.append((sym, last, vol))

    if verbose:
        print(f"[SCANNER] {len(liquid)} pairs pass volume filter (>=${min_volume_usd:,.0f} 24h)")

    if not liquid:
        return []

    # 3. Fetch recent bars and compute metrics (with concurrency cap)
    sem = asyncio.Semaphore(8)  # max 8 concurrent OHLCV requests

    async def _score_symbol(sym: str, last_px: float) -> Optional[tuple]:
        async with sem:
            try:
                ohlcv = await exchange.fetch_ohlcv(sym, timeframe="1m", limit=lookback_bars)
            except Exception as e:
                print(f"[SCANNER] {sym} OHLCV error: {e}")
                return None

        if len(ohlcv) < 50:
            return None

        closes = [float(row[4]) for row in ohlcv]
        highs  = [float(row[2]) for row in ohlcv]
        lows   = [float(row[3]) for row in ohlcv]

        # ATR(14)
        atr = _atr(highs, lows, closes, period=14)
        if atr <= 0:
            return None

        atr_pct = atr / closes[-1]
        if atr_pct < min_atr_pct:
            return None

        # EMA(50) slope normalised by ATR
        ema = _ema(closes, span=50)
        if len(ema) < 2:
            return None
        ema_slope_raw = ema[-1] - ema[-2]          # price units per bar
        ema_slope_atr = ema_slope_raw / atr if atr > 0 else 0.0

        if prefer_trending:
            score = atr_pct * (1.0 + abs(ema_slope_atr))
        else:
            score = atr_pct

        return (sym, score, atr_pct, ema_slope_atr, closes[-1])

    tasks = [_score_symbol(sym, px) for sym, px, _ in liquid]
    results = await asyncio.gather(*tasks)
    scored = [r for r in results if r is not None]
    scored.sort(key=lambda x: x[1], reverse=True)

    if verbose:
        print(f"[SCANNER] Top {min(top_n, len(scored))} of {len(scored)} scored pairs:")
        for sym, score, atr_pct, slope, px in scored[:top_n]:
            direction = "↑" if slope > 0.05 else ("↓" if slope < -0.05 else "→")
            print(f"  {sym:<16} score={score:.4f}  atr%={atr_pct*100:.3f}%  "
                  f"ema_slope_atr={slope:+.3f} {direction}  px={px:.4f}")

    return [sym for sym, *_ in scored[:top_n]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ema(values: list, span: int) -> list:
    if len(values) < span:
        return []
    k = 2.0 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    # Wilder smoothing (same as backtest engine)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

async def _main():
    import os
    from dotenv import load_dotenv
    import ccxt.pro as ccxt_pro

    load_dotenv()
    api_key = os.getenv("BINANCEUS_API_KEY", "")
    secret  = os.getenv("BINANCEUS_SECRET", "")

    exchange = ccxt_pro.binanceus({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    try:
        top = await scan_top_symbols(
            exchange,
            top_n=10,
            min_volume_usd=50_000,
            min_atr_pct=0.003,
            prefer_trending=True,
        )
        print(f"\nSelected symbols: {top}")
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(_main())
