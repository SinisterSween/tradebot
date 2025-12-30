#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Download historical OHLCV data for one or more equity/ETF symbols from IBKR
and save them as CSVs in the `data/` folder.

Each CSV will look like:

    datetime,open,high,low,close,volume
    2025-01-02 14:30:00,123.45,124.00,123.10,123.80,10000
    ...

Usage examples:

    # 30 days of 1-minute bars for TSLA
    PYTHONPATH=. .venv/bin/python scripts/download_hist_equities.py \
      --symbols TSLA \
      --duration "30 D" \
      --bar-size "1 min"

    # 60 days of 1-minute bars for TSLA, SIRI, SOXS, using delayed data
    PYTHONPATH=. .venv/bin/python scripts/download_hist_equities.py \
      --symbols TSLA,SIRI,SOXS \
      --duration "60 D" \
      --bar-size "1 min" \
      --use-delayed

"""

import argparse
from pathlib import Path

from ib_insync import IB, Stock, util


def download_symbol(
    ib: IB,
    symbol: str,
    exchange: str,
    currency: str,
    duration: str,
    bar_size: str,
    what_to_show: str,
    use_rth: bool,
    out_dir: Path,
) -> Path:
    """
    Download historical bars for a single symbol and write to CSV.
    Returns the path to the CSV.
    """
    print(f"[DL] qualifying {symbol} ({exchange}/{currency})...")
    contract = Stock(symbol, exchange, currency)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(f"IB could not qualify stock {symbol}")

    contract = qualified[0]
    print(f"[DL] fetch {duration} @ {bar_size} for {symbol} (conId={contract.conId})...")

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",           # now
        durationStr=duration,     # e.g. "30 D", "90 D"
        barSizeSetting=bar_size,  # e.g. "1 min", "5 mins"
        whatToShow=what_to_show,  # "TRADES" is fine for most
        useRTH=use_rth,           # True = regular trading hours only
        formatDate=1,
        keepUpToDate=False,
    )

    if not bars:
        raise RuntimeError(f"No historical data returned for {symbol}")

    df = util.df(bars)
    # Ensure consistent column names
    # ib_insync uses 'date' for the timestamp column by default.
    df = df.rename(columns={"date": "datetime"})
    df = df[["datetime", "open", "high", "low", "close", "volume"]]

    # Save
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_bar = bar_size.replace(" ", "")
    out_path = out_dir / f"{symbol}_{safe_bar}.csv"
    df.to_csv(out_path, index=False)

    print(f"[DL] wrote {len(df)} rows to {out_path}")
    return out_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download historical equity/ETF data from IBKR to CSV.")
    p.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated list of symbols, e.g. TSLA,SIRI,SOXS",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="IBKR host (default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=7497,
        help="IBKR port (default: 7497 for paper TWS)",
    )
    p.add_argument(
        "--client-id",
        type=int,
        default=42,
        help="Client ID for this downloader session (default: 42)",
    )
    p.add_argument(
        "--exchange",
        default="SMART",
        help="Exchange to use (default: SMART)",
    )
    p.add_argument(
        "--currency",
        default="USD",
        help="Currency to use (default: USD)",
    )
    p.add_argument(
        "--duration",
        default="30 D",
        help='Duration string for IB (default: "30 D"). Examples: "7 D", "60 D", "1 Y".',
    )
    p.add_argument(
        "--bar-size",
        default="1 min",
        help='Bar size (default: "1 min"). Examples: "1 min", "5 mins", "15 mins", "1 day".',
    )
    p.add_argument(
        "--what-to-show",
        default="TRADES",
        help='whatToShow for IBKR (default: "TRADES").',
    )
    p.add_argument(
        "--use-rth",
        action="store_true",
        help="Only use regular trading hours (RTH). Default is all sessions.",
    )
    p.add_argument(
        "--use-delayed",
        action="store_true",
        help="Use delayed market data type (3) instead of real-time (1).",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data"),
        help='Output directory for CSVs (default: "data").',
    )
    return p.parse_args()


def main():
    args = parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if not symbols:
        print("[ERR] No valid symbols provided.")
        return

    ib = IB()
    print(f"[IB] connecting to {args.host}:{args.port} clientId={args.client_id}...")
    ib.connect(args.host, args.port, clientId=args.client_id)

    # Set market data type
    md_type = 3 if args.use_delayed else 1
    ib.reqMarketDataType(md_type)
    print(f"[IB] marketDataType set to {md_type} ({'DELAYED' if md_type == 3 else 'REALTIME'})")

    try:
        for sym in symbols:
            try:
                download_symbol(
                    ib=ib,
                    symbol=sym,
                    exchange=args.exchange,
                    currency=args.currency,
                    duration=args.duration,
                    bar_size=args.bar_size,
                    what_to_show=args.what_to_show,
                    use_rth=args.use_rth,
                    out_dir=args.out_dir,
                )
            except Exception as e:
                print(f"[ERR] failed for {sym}: {e}")
    finally:
        print("[IB] disconnecting...")
        ib.disconnect()


if __name__ == "__main__":
    main()
