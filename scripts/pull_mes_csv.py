# scripts/pull_mes_csv.py
from ib_insync import IB, util, ContFuture, Future
import pandas as pd, os, time
from datetime import datetime, timedelta, timezone
import argparse

WHAT = "TRADES"
BAR_SIZE = "1 min"
MAX_RETRIES = 3

def fetch_slice(ib: IB, contract, end_dt, days: int, *, use_rth: bool) -> pd.DataFrame:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            bars = ib.reqHistoricalData(
                contract=contract,
                endDateTime=end_dt,             # '' or datetime
                durationStr=f"{days} D",
                barSizeSetting=BAR_SIZE,
                whatToShow=WHAT,
                useRTH=use_rth,
                formatDate=2,
                keepUpToDate=False,
            )
            if not bars:
                return pd.DataFrame()
            df = util.df(bars)
            if df is None or df.empty:
                return pd.DataFrame()
            df.rename(columns={"date": "datetime"}, inplace=True)
            return df[["datetime", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"[ERROR] fetch_slice failed after {attempt} tries: {e}")
                return pd.DataFrame()
            print(f"[WARN] fetch_slice attempt {attempt} failed: {e}; retrying...")
            time.sleep(2.0 * attempt)

def resolve_front_month_fut(ib: IB, symbol: str, exchange: str) -> Future:
    """Resolve CONTFUT to get yyyymm, then request that specific FUT month."""
    cf = ContFuture(symbol, exchange=exchange)
    cds = ib.reqContractDetails(cf)
    if not cds:
        raise RuntimeError(f"No contract details for {symbol} CONTFUT on {exchange}. Check TWS and market data.")
    ltm = cds[0].contract.lastTradeDateOrContractMonth  # e.g., '20251219'
    yyyymm = (ltm or "")[:6]
    if not yyyymm:
        raise RuntimeError(f"Bad lastTradeDateOrContractMonth for {symbol}: {ltm}")
    fut = Future(symbol, exchange=exchange, lastTradeDateOrContractMonth=yyyymm)
    fut_cd = ib.reqContractDetails(fut)
    if not fut_cd:
        raise RuntimeError(f"No contract details for {symbol} {yyyymm} FUT.")
    return fut_cd[0].contract

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)          # paper TWS
    ap.add_argument("--client-id", type=int, default=9)
    ap.add_argument("--symbol", default="MES")
    ap.add_argument("--exchange", default="CME")
    ap.add_argument("--days", type=int, default=30)            # total target coverage
    ap.add_argument("--chunk-days", type=int, default=7)       # per request
    ap.add_argument("--sleep", type=float, default=1.2)        # seconds between requests
    ap.add_argument("--use-rth", action="store_true")
    ap.add_argument("--outfile", default=None)
    args = ap.parse_args()

    outfile = args.outfile or f"data/{args.symbol}_1m.csv"
    os.makedirs(os.path.dirname(outfile), exist_ok=True)

    ib = IB()
    print(f"[CONNECT] {args.host}:{args.port} (clientId={args.client_id})")
    ib.connect(args.host, args.port, clientId=args.client_id)
    ib.reqMarketDataType(3)  # DELAYED

    contract = resolve_front_month_fut(ib, args.symbol, args.exchange)
    print(f"[CONTRACT] {contract.localSymbol} conId={contract.conId} secType={contract.secType} LTM={contract.lastTradeDateOrContractMonth}")

    rows = []
    covered = 0
    end_dt = ""  # first call = now
    while covered < args.days:
        print(f"[PULL] {args.chunk_days} D ending at {('now' if end_dt=='' else end_dt)} …")
        df = fetch_slice(ib, contract, end_dt, args.chunk_days, use_rth=args.use_rth)
        if df.empty:
            print("[WARN] empty slice; stopping early.")
            break
        rows.append(df)

        earliest = pd.to_datetime(df["datetime"], utc=True).min().to_pydatetime()
        end_dt = earliest - timedelta(seconds=1)
        covered += args.chunk_days
        time.sleep(max(0.0, args.sleep))

    if not rows:
        raise RuntimeError("No data returned; is TWS paper running and delayed market data allowed?")

    all_df = pd.concat(rows, ignore_index=True)
    all_df["datetime"] = pd.to_datetime(all_df["datetime"], utc=True)
    all_df = all_df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    all_df.to_csv(outfile, index=False)
    print(f"[DONE] Wrote {outfile} with {len(all_df)} rows (~{min(covered, args.days)} days).")

    ib.disconnect()

if __name__ == "__main__":
    main()
