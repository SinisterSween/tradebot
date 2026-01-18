#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import csv
import os
from pathlib import Path
from datetime import datetime, timezone
import yaml

from trader.brokers.ibkr_fractional import IbkrFractional


def _load_yaml(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _append_bar(csv_path: Path, bar: dict) -> None:
    """
    bar keys: datetime, open, high, low, close, volume
    datetime should be UTC ISO string (what ibkr_fractional already emits).
    """
    header = ["datetime", "open", "high", "low", "close", "volume"]
    _ensure_parent(csv_path)
    new_file = not csv_path.exists()

    with csv_path.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        w.writerow([
            bar["datetime"],
            float(bar["open"]),
            float(bar["high"]),
            float(bar["low"]),
            float(bar["close"]),
            float(bar.get("volume", 0) or 0),
        ])


async def run_symbol_feeder(
    *,
    symbol: str,
    out_dir: Path,
    host: str,
    port: int,
    client_id: int,
    use_delayed: bool,
    preload_days: int,
    prefill_n: int,
    bar_size_secs: int,
    what_to_show: str,
):
    csv_path = out_dir / f"{symbol.upper()}_live_1m.csv"

    broker = IbkrFractional(host, port, client_id=client_id)
    await broker.connect()

    # market data mode
    broker.set_market_data_type(3 if use_delayed else 1)
    broker.enforce_market_data_guard(use_delayed=use_delayed, allow_mismatch=False)

    # qualify contract
    await broker.resolve_contract(symbol)

    last_seen = {"dt": None}
    last_print = {"ts": 0.0}
    rows_written = {"n": 0}

    async def on_bar(bar: dict):
        # dedupe by datetime (string)
        dt = bar.get("datetime")
        if not dt or dt == last_seen["dt"]:
            return
        last_seen["dt"] = dt

        _append_bar(csv_path, bar)
        rows_written["n"] += 1

        # periodic heartbeat per symbol (every ~30s)
        now = asyncio.get_running_loop().time()
        if now - last_print["ts"] >= 30:
            print(f"[FEED] {symbol} last={dt} rows_written={rows_written['n']} csv={csv_path}")
            last_print["ts"] = now

    print(f"[START] {symbol} -> {csv_path} (clientId={client_id})")

    try:
        await broker.stream_realtime_bars(
            on_bar=on_bar,
            what_to_show=what_to_show,
            bar_size_secs=bar_size_secs,
            preload_days=preload_days,
            prefill_n=prefill_n,
        )
    finally:
        await broker.disconnect()

async def run_crypto_symbol_feeder(
    *,
    symbol: str,
    out_dir: Path,
    exchange: str = "binanceus",
    api_key: str | None = None,
    secret: str | None = None,
    use_sandbox: bool = False,
    timeframe: str = "1m",
    preload_days: int = 2,
    poll_secs: float = 2.0,
    build_only: bool = False,
    limit_per_call: int = 1000,
    ):
    """
    Writes: <out_dir>/<SYMBOL>_live_1m.csv
    Row format: datetime (UTC ISO), open, high, low, close, volume

    Notes:
    - Uses ccxt async exchange.
    - Backfills last `preload_days` worth of 1m candles, then polls for new candles.
    - `symbol` must be in the exchange's market format (e.g. "BTC/USDT").
    """
    import ccxt.async_support as ccxt  # type: ignore
    import asyncio

    csv_path = out_dir / f"{symbol.replace('/', '_').upper()}_live_1m.csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Create exchange
    ex_name = (exchange or "binanceus").lower()
    ex_cls = getattr(ccxt, ex_name, None)
    if ex_cls is None:
        raise RuntimeError(f"Unknown ccxt exchange_id: {ex_name}")

    ex = ex_cls(
        {
            "apiKey": api_key or "",
            "secret": secret or "",
            "enableRateLimit": True,
            "options": {},
        }
    )

    if use_sandbox and hasattr(ex, "set_sandbox_mode"):
        ex.set_sandbox_mode(True)

    await ex.load_markets()

    if symbol not in ex.markets:
        # Some exchanges require exact casing; try to find a close match.
        candidates = [m for m in ex.markets.keys() if m.upper() == symbol.upper()]
        if candidates:
            symbol = candidates[0]
        else:
            raise RuntimeError(f"{exchange}: market not found for symbol={symbol}")

    header = ["datetime", "open", "high", "low", "close", "volume"]
    wrote_header = csv_path.exists()

    def _write_row(ts_ms: int, o: float, h: float, l: float, c: float, v: float):
        nonlocal wrote_header
        dt_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        new_file = not wrote_header
        with csv_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(header)
                wrote_header = True
            w.writerow([dt_iso, float(o), float(h), float(l), float(c), float(v)])

    # ----- Backfill -----
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = int((datetime.now(timezone.utc).timestamp() - preload_days * 86400) * 1000)

    # Aim to backfill up through the last *closed* 1m candle
    target_end_ms = now_ms - 60_000

    last_seen_ms: int | None = None
    rows_written = 0

    print(f"[START][CRYPTO] {exchange} {symbol} -> {csv_path} preload_days={preload_days}")

    try:
        # Keep paging until we reach target_end_ms or the exchange stops returning data
        while True:
            ohlcv = await ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit_per_call)
            if not ohlcv:
                break

            advanced = False

            for ts_ms, o, h, l, c, v in ohlcv:
                if last_seen_ms is not None and ts_ms <= last_seen_ms:
                    continue
                _write_row(ts_ms, o, h, l, c, v)
                last_seen_ms = ts_ms
                rows_written += 1
                advanced = True

            if last_seen_ms is None:
                break

            # If we didn't advance, stop to avoid an infinite loop on duplicate pages
            if not advanced:
                break

            # Done when we’ve reached the last closed candle
            if last_seen_ms >= target_end_ms:
                break

            # Advance since to just after the last candle we wrote
            since_ms = last_seen_ms + 60_000


        print(f"[HIST][CRYPTO] {symbol} backfill rows={rows_written} last_seen={last_seen_ms}")

        if build_only:
            # stop here — this script is for building backtest CSVs
            return
        # ----- Live poll -----
        last_print = 0.0
        while True:
            # Get most recent candles (2 gives you the just-closed candle reliably)
            ohlcv = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=2)
            if ohlcv:
                for ts_ms, o, h, l, c, v in ohlcv:
                    if last_seen_ms is None or ts_ms > last_seen_ms:
                        _write_row(ts_ms, o, h, l, c, v)
                        last_seen_ms = ts_ms
                        rows_written += 1

            now = asyncio.get_running_loop().time()
            if now - last_print >= 30:
                last_dt = (
                    datetime.fromtimestamp(last_seen_ms / 1000, tz=timezone.utc).isoformat()
                    if last_seen_ms
                    else "?"
                )
                print(f"[FEED][CRYPTO] {symbol} last={last_dt} rows_written={rows_written} csv={csv_path}")
                last_print = now

            await asyncio.sleep(poll_secs)

    finally:
        await ex.close()


def _universe_assets(universe_path: str) -> dict:
    uni = _load_yaml(universe_path)

    asset_class = str(uni.get("asset_class") or "equity").lower()
    exchange = str(uni.get("exchange") or "").lower()

    raw_symbols = uni.get("symbols") or []
    symbols: list[str] = []

    for item in raw_symbols:
        # Case 1: plain list of strings: ["SPY", "QQQ"] or ["ETH/USDT", "SOL/USDT"]
        if isinstance(item, str):
            sym = item.strip()
            if sym:
                symbols.append(sym)
            continue

        # Case 2: list of dicts: [{"symbol": "ETH/USDT", "strategy": {...}}, ...]
        if isinstance(item, dict):
            sym = (item.get("symbol") or item.get("SYMBOL") or "").strip()
            if sym:
                symbols.append(sym)
            continue

        # ignore anything else
        continue

    # normalize symbols:
    # - crypto should keep original casing and "/" for ccxt
    # - non-crypto we can uppercase to match your existing behavior
    if asset_class != "crypto":
        symbols = [s.upper() for s in symbols]

    backfill = uni.get("backfill") or {}
    preload_days = int(backfill.get("days", uni.get("preload_days", 2)))
    timeframe = str(backfill.get("timeframe", "1m"))
    limit_per_call = int(backfill.get("limit_per_call", 1000))

    return {
        "asset_class": asset_class,
        "exchange": exchange,
        "symbols": symbols,
        "preload_days": preload_days,
        "timeframe": timeframe,
        "limit_per_call": limit_per_call,
    }




async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universes", nargs="+", required=True,
                    help="Universe names or paths. Example: sp500_fractional or config/universes/sp500_fractional.yaml")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id-start", type=int, default=50,
                    help="ClientId base. Each symbol increments from here.")
    ap.add_argument("--build-only", action="store_true",
                help="Build historical CSVs then exit (no live polling/streaming).")
    ap.add_argument("--delayed", action="store_true", help="Use delayed market data (reqMarketDataType=3)")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--preload-days", type=int, default=2)
    ap.add_argument("--prefill-n", type=int, default=2000)
    ap.add_argument("--bar-size-secs", type=int, default=60)
    ap.add_argument("--what-to-show", default="TRADES")
    ap.add_argument("--max-concurrency", type=int, default=6,
                    help="How many symbols to stream at once (IB/TWS load control).")
    ap.add_argument("--reset-csv", action="store_true",
                help="Delete existing per-symbol CSV before writing (fresh rebuild).")

    args = ap.parse_args()

    # resolve universe paths
    universe_paths = []
    for u in args.universes:
        if u.endswith(".yaml") and os.path.exists(u):
            universe_paths.append(u)
        else:
            p = os.path.join("config", "universes", f"{u}.yaml")
            universe_paths.append(p)

    # expand universe entries (asset_class + symbols)
    entries = []
    for up in universe_paths:
        meta = _universe_assets(up)
        for s in meta["symbols"]:
            entries.append({
                "symbol": s,
                "asset_class": meta["asset_class"],
                "exchange": meta["exchange"],
                "preload_days": meta.get("preload_days", args.preload_days),
                "timeframe": meta.get("timeframe", "1m"),
                "limit_per_call": meta.get("limit_per_call", 1000),
            })

    # de-dupe by (asset_class, exchange, symbol) while preserving order
    seen = set()
    deduped = []
    for e in entries:
        key = (e["asset_class"], e["exchange"], e["symbol"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    entries = deduped

    if not entries:
        raise SystemExit("No symbols found in the provided universes.")

    print(f"[PLAN] universes={len(universe_paths)} symbols={len(entries)} out_dir={args.out_dir}")
    out_dir = Path(args.out_dir)

    sem = asyncio.Semaphore(args.max_concurrency)
    import ast
    async def _run_one(idx: int, e: dict):
        async with sem:
            def _extract_symbol(e: dict) -> str:
                """
                e can be:
                - normal: {"symbol": "ETH/USDT", ...}
                - sometimes broken: {"symbol": "{'SYMBOL': 'ETH_USDT', ...}", ...}
                - sometimes alternate: {"SYMBOL": "ETH_USDT", ...}
                """
                sym = e.get("symbol") or e.get("SYMBOL")

                # If sym is already a dict for some reason
                if isinstance(sym, dict):
                    sym = sym.get("symbol") or sym.get("SYMBOL")

                # If sym is a stringified dict like "{'SYMBOL': 'ETH_USDT', ...}"
                if isinstance(sym, str):
                    s = sym.strip()
                    if s.startswith("{") and ("SYMBOL" in s or "symbol" in s):
                        try:
                            d = ast.literal_eval(s)
                            if isinstance(d, dict):
                                sym = d.get("symbol") or d.get("SYMBOL")
                        except Exception:
                            pass

                if not isinstance(sym, str) or not sym.strip():
                    raise ValueError(f"[CSV] could not extract symbol from entry: {e}")

                return sym.strip()

            sym = _extract_symbol(e)

            if e.get("asset_class") == "crypto":
                # allow either "ETH/USDT" or "ETH_USDT" etc
                sym_fs = sym.replace("/", "_").replace("-", "_").replace(":", "_").upper()
                print(f"[DBG] entry_symbol_type={type(e.get('symbol'))} entry_symbol={repr(e.get('symbol'))[:120]}")
                csv_path = out_dir / f"{sym_fs}_live_1m.csv"

                if args.reset_csv:
                    # avoid stat() on some weird long path edge cases
                    try:
                        if csv_path.exists():
                            csv_path.unlink()
                            print(f"[RESET] deleted {csv_path}")
                    except OSError as ex:
                        raise OSError(f"[RESET] bad csv_path={csv_path} sym={sym} entry_symbol={e.get('symbol')}") from ex

                preload_days = int(e.get("preload_days", args.preload_days))
                timeframe = str(e.get("timeframe", "1m"))
                limit_per_call = int(e.get("limit_per_call", 1000))

                await run_crypto_symbol_feeder(
                    symbol=sym,
                    out_dir=out_dir,
                    exchange=(e.get("exchange") or "binanceus"),
                    timeframe=timeframe,
                    preload_days=preload_days,
                    limit_per_call=limit_per_call,
                    poll_secs=2.0,
                    build_only=bool(args.build_only),
                )
                return  # important: don’t fall through to non-crypto
            
            # non-crypto
            await run_symbol_feeder(
                symbol=sym,
                out_dir=out_dir,
                host=args.host,
                port=args.port,
                client_id=args.client_id_start + idx,
                use_delayed=bool(args.delayed),
                preload_days=args.preload_days,
                prefill_n=args.prefill_n,
                bar_size_secs=args.bar_size_secs,
                what_to_show=args.what_to_show,
            )

    tasks = [asyncio.create_task(_run_one(i, e)) for i, e in enumerate(entries)]



    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] KeyboardInterrupt -> exiting")
