# scripts/shadow_mes_delayed.py
from ib_insync import *
import pandas as pd, os, time

HOST = "127.0.0.1"
PORT = 7497            # paper TWS
CLIENT_ID = 9
WHAT = "TRADES"
BAR_SIZE = "1 min"
USE_RTH = False        # True if you want only RTH bars
LOOKBACK_MIN = 180     # pull last 3h each loop; dedupe on write
SLEEP_SEC = 60         # cadence

OUTFILE = "data/MES_live_1m.csv"

def ensure_dirs(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)

def df_from_bars(bars):
    df = util.df(bars)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"date": "datetime"})
    df = df[["datetime","open","high","low","close","volume"]]
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    return df

def resolve_front_month(ib: IB):
    """
    Resolve the continuous MES to the current front-month FUT
    and return the specific tradable Future (not CONTFUT).
    """
    cf = ContFuture("MES", exchange="CME")
    cds = ib.reqContractDetails(cf)
    if not cds:
        raise RuntimeError("No contract details for MES continuous.")
    ltm = cds[0].contract.lastTradeDateOrContractMonth or ""
    yyyymm = ltm[:6]
    if not yyyymm:
        raise RuntimeError(f"Bad LTM from CONTFUT: {ltm}")
    fut = Future("MES", exchange="CME", lastTradeDateOrContractMonth=yyyymm)
    fut_cd = ib.reqContractDetails(fut)
    if not fut_cd:
        raise RuntimeError(f"No details for MES {yyyymm} FUT.")
    return fut_cd[0].contract

def main():
    ensure_dirs(OUTFILE)
    ib = IB()
    print(f"[CONNECT] {HOST}:{PORT} clientId={CLIENT_ID}")
    ib.connect(HOST, PORT, clientId=CLIENT_ID)
    ib.reqMarketDataType(3)  # 3 = DELAYED

    con = resolve_front_month(ib)
    print(f"[CONTRACT] {con.localSymbol} conId={con.conId} LTM={con.lastTradeDateOrContractMonth}")

    # Load or init CSV
    if os.path.exists(OUTFILE):
        all_df = pd.read_csv(OUTFILE)
        if not all_df.empty:
            all_df["datetime"] = pd.to_datetime(all_df["datetime"], utc=True)
    else:
        all_df = pd.DataFrame(columns=["datetime","open","high","low","close","volume"])

    KEEP_DAYS= int(os.environ.get("KEEP_DAYS", "90"))
    duration_str = f"{LOOKBACK_MIN * 60} S"  # IB duration units are S/D/W/M/Y

    try:
        while True:
            try:
                # Reconnect if needed
                if not ib.isConnected():
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    time.sleep(1.0)
                    ib.connect(HOST, PORT, clientId=CLIENT_ID)
                    ib.reqMarketDataType(3)

                bars = ib.reqHistoricalData(
                    con,
                    endDateTime="",               # now
                    durationStr=duration_str,     # e.g., "10800 S" for 3h
                    barSizeSetting=BAR_SIZE,      # "1 min"
                    whatToShow=WHAT,              # "TRADES"
                    useRTH=USE_RTH,
                    formatDate=2,
                    keepUpToDate=False,
                )

                df = df_from_bars(bars)
                if df.empty:
                    print("[WARN] Empty slice; will retry next tick.")
                else:
                    last_old = pd.Timestamp.min if all_df.empty else all_df["datetime"].max()
                    last_new = df["datetime"].max()
                    if last_new <= last_old:
                        print(f"[NOOP] No new bars (last={last_old}); market may be closed.")
                    else:
                        new_rows = int((df["datetime"] > last_old).sum())
                        if all_df.empty:
                            all_df = df.copy()
                        else:
                            all_df = (
                                pd.concat([all_df, df], ignore_index=True)
                                .drop_duplicates(subset=["datetime"])
                                .sort_values("datetime")
                                .reset_index(drop=True)
                            )
                        if KEEP_DAYS > 0:
                
                            cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=KEEP_DAYS)
                            all_df = all_df[all_df["datetime"] >= cutoff]
                        # Always write after each loop
                        all_df.to_csv(OUTFILE, index=False)
                        print(f"[WRITE] +{new_rows} new "
                            f"(last={all_df['datetime'].iloc[-1]}) -> {OUTFILE} [total {len(all_df)}]")

            except Exception as e:
                print(f"[WARN] shadow loop error: {e}; sleeping {SLEEP_SEC}s…")

            # Pace the loop on both success and error
            time.sleep(SLEEP_SEC)

    except KeyboardInterrupt:
        print("\n[STOP] keyboard interrupt; disconnecting.")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    main()
