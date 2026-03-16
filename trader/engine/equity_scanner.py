"""
Equity scanner for IBKR (fractional/stock broker).

Fetches recent 1m bars for every symbol in a universe, computes ATR(14),
EMA(50) slope, and overnight gap, then returns a ranked list of the best
candidates for the day's trade.

Scoring (prefer_trending=True):
    score = atr_pct * (1 + abs(ema_slope_atr)) * (1 + abs(gap_pct) * 5)
    Favours stocks that have both volatility AND recent trend direction — ideal
    for HybridOrbVwap which needs directional momentum to break the ORB range.

Usage (from run_live.py):
    from trader.engine.equity_scanner import scan_top_symbols
    symbols = await scan_top_symbols(broker, ["NVDA","MSFT","SPY",...], top_n=3)
"""

import asyncio
from typing import List, Optional


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------

async def scan_top_symbols(
    broker,
    symbols: List[str],
    *,
    top_n: int = 3,
    lookback_days: int = 3,          # days of 1m history to pull per symbol
    prefer_trending: bool = True,    # include EMA slope in score
    inter_request_sleep: float = 1.5, # seconds between IBKR historical requests
    verbose: bool = True,
) -> List[str]:
    """
    Returns up to top_n symbols from `symbols` ranked by trading quality.

    Requires broker to already be connected (broker.ib is live).
    Uses reqHistoricalDataAsync directly — does NOT call resolve_contract().
    """
    try:
        from ib_async import Stock
    except ImportError:
        try:
            from ib_insync import Stock
        except ImportError:
            print("[EQUITY-SCANNER] ib_async / ib_insync not available — skipping scan")
            return symbols[:top_n]

    if verbose:
        print(f"[EQUITY-SCANNER] scanning {len(symbols)} symbols (lookback={lookback_days}d)")

    scored = []

    for sym in symbols:
        try:
            contract = Stock(sym, "SMART", "USD")
            bars = await broker.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=f"{lookback_days} D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
            )
            await asyncio.sleep(inter_request_sleep)

            if not bars or len(bars) < 50:
                if verbose:
                    print(f"[EQUITY-SCANNER] {sym}: insufficient bars ({len(bars) if bars else 0}) — skip")
                continue

            closes = [float(b.close) for b in bars]
            highs  = [float(b.high)  for b in bars]
            lows   = [float(b.low)   for b in bars]

            # ATR(14) — Wilder smoothing
            atr = _atr(highs, lows, closes, period=14)
            if atr <= 0 or closes[-1] <= 0:
                continue

            atr_pct = atr / closes[-1]

            # EMA(50) slope normalised by ATR
            ema = _ema(closes, span=50)
            ema_slope_atr = ((ema[-1] - ema[-2]) / atr) if len(ema) >= 2 else 0.0

            # Overnight gap: scan for the most recent session boundary
            # (gap between consecutive bars > 60 min = close of one RTH session,
            #  open of the next)
            gap_pct = 0.0
            try:
                dates = [b.date for b in bars]
                for i in range(len(bars) - 1, 0, -1):
                    dt_curr = dates[i]
                    dt_prev = dates[i - 1]
                    gap_mins = (dt_curr - dt_prev).total_seconds() / 60.0
                    if gap_mins > 60:
                        gap_pct = (bars[i].open - bars[i - 1].close) / bars[i - 1].close
                        break
            except Exception:
                pass  # gap stays 0 if date parse fails

            if prefer_trending:
                score = atr_pct * (1.0 + abs(ema_slope_atr)) * (1.0 + abs(gap_pct) * 5.0)
            else:
                score = atr_pct * (1.0 + abs(gap_pct) * 5.0)

            scored.append((sym, score, atr_pct, ema_slope_atr, gap_pct, closes[-1]))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if verbose:
                print(f"[EQUITY-SCANNER] {sym} error: {e}")

    scored.sort(key=lambda x: x[1], reverse=True)

    if verbose:
        print(f"[EQUITY-SCANNER] Top {min(top_n, len(scored))} of {len(scored)} scored symbols:")
        for sym, score, atr_pct, slope, gap, px in scored[:top_n]:
            direction = "↑" if slope > 0.05 else ("↓" if slope < -0.05 else "→")
            gap_str = f"gap={gap * 100:+.2f}%"
            print(f"  {sym:<8} score={score:.4f}  atr%={atr_pct * 100:.3f}%  "
                  f"ema_slope_atr={slope:+.3f}{direction}  {gap_str}  px={px:.2f}")

    return [sym for sym, *_ in scored[:top_n]]


# ---------------------------------------------------------------------------
# Helpers (same implementations as crypto_scanner for consistency)
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
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr
