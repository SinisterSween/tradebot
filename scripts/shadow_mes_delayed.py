#!/usr/bin/env python3
from ib_insync import *
import pandas as pd, os, time, argparse, sys
from pathlib import Path

# ---- defaults ----
DEF_HOST         = os.getenv("IB_HOST", "127.0.0.1")
DEF_PORT         = int(os.getenv("IB_PORT", "7497"))
DEF_CLIENT_ID    = int(os.getenv("IB_CLIENT_ID", "9"))
DEF_SYMBOL       = os.getenv("SYMBOL", "MES").upper()
DEF_EXCHANGE     = os.getenv("EXCHANGE", "CME")
DEF_WHAT         = os.getenv("WHAT", "TRADES")
DEF_BAR_SIZE     = os.getenv("BAR_SIZE", "1 min")
DEF_USE_RTH      = os.getenv("USE_RTH", "false").lower() in ("1", "true", "yes")
DEF_LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "180"))
DEF_SLEEP_SEC    = int(os.getenv("SLEEP_SEC", "60"))
DEF_KEEP_DAYS    = int(os.getenv("KEEP_DAYS", "90"))
DEF_OUTFILE      = os.getenv("OUTFILE", f"data/{DEF_SYMBOL}_live_1m.csv")


import pandas as pd

def to_utc_aware(x):
    """
    Make *anything* UTC tz-aware:
    - Series
    - DatetimeIndex
    - single Timestamp
    - scalar datetime
    """
    v = pd.to_datetime(x, errors="coerce")

    # Series / Index
    if hasattr(v, "dt"):
        # v is Series-like
        if v.dt.tz is None:
            return v.dt.tz_localize("UTC")
        else:
            return v.dt.tz_convert("UTC")

    # single Timestamp
    if isinstance(v, pd.Timestamp):
        if v.tz is None:
            return v.tz_localize("UTC")
        else:
            return v.tz_convert("UTC")

    return v


def log(msg: str, symbol: str):
    p = Path(f"logs/shadow_{symbol.lower()}.out")
    p.parent.mkdir(parents=True, exist_ok=True)
    line = msg if msg.endswith("\n") else msg + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    with p.open("a") as f:
        f.write(line)


def ensure_dirs(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)



def df_from_bars(bars):
    df = util.df(bars)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"date": "datetime"})
    df = df[["datetime","open","high","low","close","volume"]]
    df["datetime"] = to_utc_aware(df["datetime"])
    return df


def resolve_front_month(ib: IB, symbol: str, exchange: str):
    # try continuous first
    cf = ContFuture(symbol, exchange=exchange)
    cds = ib.reqContractDetails(cf)
    if cds:
        ltm = cds[0].contract.lastTradeDateOrContractMonth or ""
        yyyymm = ltm[:6]
        if yyyymm:
            fut = Future(symbol, exchange=exchange, lastTradeDateOrContractMonth=yyyymm)
            fut_cd = ib.reqContractDetails(fut)
            if fut_cd:
                return fut_cd[0].contract
    # fallback: get a list of futures and pick the nearest
    f = Future(symbol, exchange=exchange)
    cds2 = ib.reqContractDetails(f)
    if cds2:
        cds2.sort(key=lambda c: c.contract.lastTradeDateOrContractMonth or "999999")
        return cds2[0].contract
    raise RuntimeError(f"Cannot resolve front month for {symbol} on {exchange}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=DEF_SYMBOL)
    ap.add_argument("--exchange", default=DEF_EXCHANGE)
    ap.add_argument("--out", default=DEF_OUTFILE)
    ap.add_argument("--host", default=DEF_HOST)
    ap.add_argument("--port", type=int, default=DEF_PORT)
    ap.add_argument("--clientId", type=int, default=DEF_CLIENT_ID)
    ap.add_argument("--what", default=DEF_WHAT)
    ap.add_argument("--bar-size", default=DEF_BAR_SIZE)
    ap.add_argument("--use-rth", action="store_true", default=DEF_USE_RTH)
    ap.add_argument("--lookback-min", type=int, default=DEF_LOOKBACK_MIN)
    ap.add_argument("--sleep-sec", type=int, default=DEF_SLEEP_SEC)
    ap.add_argument("--keep-days", type=int, default=DEF_KEEP_DAYS)
    args = ap.parse_args()

    symbol = args.symbol.upper()
    ensure_dirs(args.out)

    ib = IB()
    log(f"[CONNECT] {args.host}:{args.port} clientId={args.clientId} symbol={symbol}", symbol)
    ok = ib.connect(args.host, args.port, clientId=args.clientId, timeout=5)
    if not ok:
        log("[FATAL] could not connect to IB/TWS", symbol)
        return
    ib.reqMarketDataType(3)

    try:
        con = resolve_front_month(ib, symbol, args.exchange)
        log(f"[CONTRACT] {con.localSymbol} conId={con.conId} LTM={con.lastTradeDateOrContractMonth}", symbol)
    except Exception as e:
        log(f"[FATAL] could not resolve {symbol} on {args.exchange}: {e}", symbol)
        return

    # load existing
    if os.path.exists(args.out):
        all_df = pd.read_csv(args.out)
        if not all_df.empty:
            all_df["datetime"] = to_utc_aware(all_df["datetime"])
    else:
        all_df = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    duration_str = f"{args.lookback_min * 60} S"

    try:
        while True:
            try:
                if not ib.isConnected():
                    log("[RECONNECT] reconnecting…", symbol)
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    time.sleep(1.0)
                    ib.connect(args.host, args.port, clientId=args.clientId)
                    ib.reqMarketDataType(3)

                bars = ib.reqHistoricalData(
                    con,
                    endDateTime="",
                    durationStr=duration_str,
                    barSizeSetting=args.bar_size,
                    whatToShow=args.what,
                    useRTH=args.use_rth,
                    formatDate=2,
                    keepUpToDate=False,
                )

                df = df_from_bars(bars)  # this must already call to_utc_aware
                if df.empty:
                    log("[WARN] Empty slice; will retry…", symbol)
                else:
                    # 1) normalize existing
                    if not all_df.empty:
                        all_df["datetime"] = to_utc_aware(all_df["datetime"])

                    # 2) get last_old and last_new using ONLY the helper
                    if all_df.empty:
                        last_old = pd.Timestamp.min  # <- NAIVE on purpose
                        last_old = to_utc_aware(last_old)  # <- now UTC aware
                    else:
                        last_old = to_utc_aware(all_df["datetime"].max())

                    last_new = to_utc_aware(df["datetime"].max())

                    # 3) compare
                    if last_new <= last_old:
                        log(f"[NOOP] No new bars (last={last_old})", symbol)
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

                        # 4) prune with UTC-aware cutoff
                        if args.keep_days > 0:
                            cutoff = pd.Timestamp.utcnow()
                            cutoff = to_utc_aware(cutoff) - pd.Timedelta(days=args.keep_days)
                            all_df = all_df[all_df["datetime"] >= cutoff]

                        all_df.to_csv(args.out, index=False)
                        log(f"[WRITE] {symbol} +{new_rows} -> {args.out} [total {len(all_df)}]", symbol)

            except Exception as e:
                log(f"[WARN] shadow loop error for {symbol}: {e}", symbol)

            time.sleep(args.sleep_sec)


    except KeyboardInterrupt:
        log("[STOP] keyboard interrupt; disconnecting", symbol)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
