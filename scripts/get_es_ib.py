from ib_insync import *
import pandas as pd
from datetime import datetime

HOST, PORT, CLIENT_ID = "127.0.0.1", 7497, 100

def resolve_es_specific(ib: IB) -> Contract:
    """
    Return a specific-month ES Future on CME (not a ContFuture),
    preferring the current front month.
    """
    # Try continuous just to discover the active month, then convert to specific future
    try:
        cf = ContFuture("MES", exchange="CME")
        q = ib.qualifyContracts(cf)
        if q:
            # e.g., '20250919' -> '202509'
            month6 = (q[0].lastTradeDateOrContractMonth or "")[:6]
            if month6 and month6.isdigit():
                fut = Future(symbol="MES",
                             lastTradeDateOrContractMonth=month6,
                             exchange="CME",
                             currency="USD")
                qq = ib.qualifyContracts(fut)
                if qq:
                    return qq[0]
    except Exception:
        pass

    # Fallback: scan contract details for the nearest >= current month
    cds = ib.reqContractDetails(Future(symbol="MES", exchange="CME"))
    if not cds:
        raise RuntimeError("No ES contract details returned from IB.")

    today_m = datetime.utcnow().strftime("%Y%m")  # 'YYYYMM'
    candidates = []
    for cd in cds:
        con = cd.contract
        lm = (getattr(con, "lastTradeDateOrContractMonth", "") or "")[:6]
        if len(lm) == 6 and lm.isdigit():
            candidates.append((lm, con))
    # pick the nearest contract month >= today_m; else pick the earliest future month
    fwd = [c for c in candidates if c[0] >= today_m]
    chosen = (sorted(fwd, key=lambda x: x[0])[0] if fwd
              else sorted(candidates, key=lambda x: x[0])[0])
    qq = ib.qualifyContracts(chosen[1])
    if not qq:
        raise RuntimeError("Failed to qualify ES specific-month contract.")
    return qq[0]

def main():
    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    ib.reqMarketDataType(3)  # 1=real-time, 3=delayed (works without CME realtime)

    contract = resolve_es_specific(ib)
    print("Using contract:", contract)

    dfs = []
    end = ""  # page backward one day at a time
    for _ in range(5):
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=end,
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                keepUpToDate=False,
            )
        except Exception as e:
            print("Historical request failed:", e)
            break

        df = util.df(bars)
        if df is None or df.empty:
            break
        dfs.append(df)
        end = df.iloc[0].date  # step back a day

    ib.disconnect()

    if not dfs:
        print("No data returned. Check CME market data permissions or try during RTH.")
        return

    data = (pd.concat(dfs)
              .drop_duplicates(subset=["date"])
              .sort_values("date"))
    data.rename(columns=str.lower, inplace=True)
    data.rename(columns={"date": "datetime"}, inplace=True)
    out = data[["datetime","open","high","low","close","volume"]]
    out.to_csv("data/ES_1m.csv", index=False)
    print("Wrote data/ES_1m.csv:", len(out))

if __name__ == "__main__":
    main()
