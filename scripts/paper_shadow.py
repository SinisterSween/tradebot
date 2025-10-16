# scripts/paper_shadow.py
import os, sys, time, argparse, yaml, pandas as pd
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from ib_insync import IB, util, ContFuture, Future

# reuse your engine pieces
from trader.engine.backtest import prepare_bars
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig

# -------- helpers (mirrors your run_backtest.py bits) --------
def _deep_merge(a, b):
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def _norm_windows(windows):
    out = []
    for w in (windows or []):
        if isinstance(w, dict):
            out.append({"start": str(w.get("start")), "end": str(w.get("end"))})
        elif isinstance(w, (list, tuple)) and len(w) == 2:
            out.append({"start": str(w[0]), "end": str(w[1])})
        else:
            raise ValueError(f"Bad session_windows entry: {w!r}")
    return out

def _minutes_to_window_end(ts_local_str, windows):
    try:
        hh, mm = map(int, str(ts_local_str)[:5].split(":"))
    except Exception:
        return 0
    cur = hh*60 + mm
    for w in windows:
        s_h, s_m = map(int, str(w["start"]).split(":"))
        e_h, e_m = map(int, str(w["end"]).split(":"))
        if (s_h*60+s_m) <= cur <= (e_h*60+e_m):
            return (e_h*60+e_m) - cur
    return 9999

def resolve_front_month_fut(ib: IB, symbol: str, exchange: str) -> Future:
    cf = ContFuture(symbol, exchange=exchange)
    cds = ib.reqContractDetails(cf)
    if not cds:
        raise RuntimeError(f"No contract details for {symbol} CONTFUT on {exchange}.")
    ltm = cds[0].contract.lastTradeDateOrContractMonth  # e.g. '20251219'
    yyyymm = (ltm or "")[:6]
    if not yyyymm:
        raise RuntimeError(f"Bad lastTradeDateOrContractMonth for {symbol}: {ltm}")
    fut = Future(symbol, exchange=exchange, lastTradeDateOrContractMonth=yyyymm)
    fut_cd = ib.reqContractDetails(fut)
    if not fut_cd:
        raise RuntimeError(f"No contract details for {symbol} {yyyymm} FUT.")
    return fut_cd[0].contract

# -------- main --------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts", default="config/contracts.yaml")
    ap.add_argument("--profile",   default="config/profile/futures.yaml")
    ap.add_argument("--env",       default="config/env/paper.yaml")
    ap.add_argument("--config",    default="config/overrides/clean_v1.yaml")
    ap.add_argument("--symbol",    default="MES")
    ap.add_argument("--exchange",  default="CME")
    ap.add_argument("--host",      default="127.0.0.1")
    ap.add_argument("--port",      type=int, default=7497)   # TWS paper
    ap.add_argument("--client-id", type=int, default=42)
    ap.add_argument("--use-rth",   action="store_true")
    ap.add_argument("--logfile",   default="logs/shadow_signals.csv")
    args = ap.parse_args()

    os.makedirs("logs", exist_ok=True)

    # ---- Load layered config: profile -> env -> overrides ----
    cfg = {}
    if args.profile and os.path.exists(args.profile):
        cfg = _deep_merge(cfg, yaml.safe_load(open(args.profile, "r")) or {})
    if args.env and os.path.exists(args.env):
        cfg = _deep_merge(cfg, yaml.safe_load(open(args.env, "r")) or {})
    if args.config and os.path.exists(args.config):
        cfg = _deep_merge(cfg, yaml.safe_load(open(args.config, "r")) or {})

    # contracts/fees
    contracts = yaml.safe_load(open(args.contracts, "r"))
    ct = contracts[args.symbol]
    cfg.setdefault("fees", {})
    for k in ("tick_size","tick_value","commission_per_contract","exchange_fees_per_contract"):
        if k in ct: cfg["fees"][k] = ct[k]
    tick_size = float(cfg["fees"]["tick_size"])
    tick_val  = float(cfg["fees"]["tick_value"])

    # strategy + risk wires
    sc = cfg["strategy"]
    windows = _norm_windows(sc.get("session_windows", []))
    tz = cfg.get("timezone", "America/Chicago")

    strat = HybridOrbVwap(StratConfig(
        orb_minutes=sc["orb_minutes"],
        atr_len=sc["atr_len"],
        break_eps_ticks=sc["break_eps_ticks"],
        atr_mult=sc["atr_mult"],
        target_R=sc["target_R"],
        slope_min=sc.get("slope_min", 0.0),
        stop_pad_ticks=sc["stop_pad_ticks"],
        trail_pad_ticks=sc["trail_pad_ticks"],
        tick_size=tick_size,
        tick_value=tick_val,
    ))

    fees = cfg["fees"]
    risk_cfg = RiskConfig(
        account_equity=cfg["risk"]["account_equity"],
        risk_pct=cfg["risk"]["risk_pct"],
        max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
        max_consec_losses=cfg["risk"]["max_consec_losses"],
        tick_value=fees["tick_value"],
        flat_time=cfg["risk"]["flat_time"],
        news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
    )
    risk = RiskGovernor(risk_cfg)
    risk.risk_buffer_pct = float(cfg.get("risk", {}).get("risk_buffer_pct", 1.0))
    risk.max_contracts_per_trade = int(cfg["risk"].get("max_contracts_per_trade", 0)) or None

    # execution knobs (for gating; we don't place orders here)
    exe = cfg.setdefault("execution", {})
    MIN_WIN_END  = int(exe.get("min_minutes_to_window_end", 35))
    cooldown_bars = int(exe.get("cooldown_bars", 15))

    # ---- IB connect + subscription ----
    ib = IB()
    print(f"[CONNECT] {args.host}:{args.port} (clientId={args.client_id})")
    ib.connect(args.host, args.port, clientId=args.client_id)
    ib.reqMarketDataType(3)  # 3 = delayed if live not available; paper usually has live

    contract = resolve_front_month_fut(ib, args.symbol, args.exchange)
    print(f"[CONTRACT] {contract.localSymbol} conId={contract.conId} LTM={contract.lastTradeDateOrContractMonth}")

    # real-time 5-sec bars
    rt = ib.reqRealTimeBars(contract, barSize=5, whatToShow="TRADES", useRTH=args.use_rth)
    print("[STREAM] Subscribed to 5s bars … aggregating to 1m")

    # shadow minute buffer + last signal time to respect cooldown
    minute_buf = []     # holds current minute's 5s bars
    last_emitted_min = None
    last_signal_idx = -10_000

    # ensure logfile has header
    if not os.path.exists(args.logfile):
        pd.DataFrame(columns=[
            "ts","t_local","reason","side","qty","entry","stop","target","atr_mult",
            "target_R","break_eps_ticks","slope_min","cooldown_bars"
        ]).to_csv(args.logfile, index=False)

    tz_chi = ZoneInfo(tz)

    def on_new_5s_bar(b):
        nonlocal minute_buf, last_emitted_min, last_signal_idx

        # IB bar time is timezone-aware (UTC); normalize and also derive local time
        ts_utc = pd.Timestamp(b.time, tz="UTC")
        ts_local = ts_utc.tz_convert(tz_chi)

        # group by clock minute
        minute_key = ts_local.replace(second=0, microsecond=0)

        # new minute boundary => finalize previous minute
        if last_emitted_min is not None and minute_key > last_emitted_min:
            # aggregate minute_buf to 1m ohlc
            if minute_buf:
                mdf = pd.DataFrame(minute_buf)
                row = {
                    "datetime":  mdf["datetime"].iloc[0],
                    "open":      float(mdf["open"].iloc[0]),
                    "high":      float(mdf["high"].max()),
                    "low":       float(mdf["low"].min()),
                    "close":     float(mdf["close"].iloc[-1]),
                    "volume":    float(mdf["volume"].sum()),
                }
                # build a day dataframe for prepare_bars()
                day_df = pd.DataFrame([row])
                # keep recent history by reading what we already built today (append)
                # For simplicity, build an in-memory trail
                on_new_5s_bar.df_minute = getattr(on_new_5s_bar, "df_minute", pd.DataFrame(columns=day_df.columns))
                on_new_5s_bar.df_minute = pd.concat([on_new_5s_bar.df_minute, pd.DataFrame([row])], ignore_index=True)
                # drop dups, sort
                on_new_5s_bar.df_minute = on_new_5s_bar.df_minute.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

                # prepare + signal on the **closed** minute
                df_prep = prepare_bars(on_new_5s_bar.df_minute.copy(), tz, sc["orb_minutes"])
                bar = df_prep.iloc[-1].to_dict()

                # trading window + cooldown checks (advisory only)
                if risk.can_trade_now(bar["t_local"]) and _minutes_to_window_end(bar["t_local"], windows) >= MIN_WIN_END:
                    # simple bar-cooldown: suppress signals within N bars after the last one
                    cur_idx = len(df_prep) - 1
                    cool_ok = (cur_idx - last_signal_idx) >= cooldown_bars

                    if cool_ok:
                        sig = HybridOrbVwap(StratConfig(
                            orb_minutes=sc["orb_minutes"],
                            atr_len=sc["atr_len"],
                            break_eps_ticks=sc["break_eps_ticks"],
                            atr_mult=sc["atr_mult"],
                            target_R=sc["target_R"],
                            slope_min=sc.get("slope_min", 0.0),
                            stop_pad_ticks=sc["stop_pad_ticks"],
                            trail_pad_ticks=sc["trail_pad_ticks"],
                            tick_size=tick_size,
                            tick_value=tick_val,
                        )).maybe_signal(bar, windows, risk)

                        if sig:
                            side = str(sig["order"].side).upper()
                            qty  = int(sig["order"].qty)
                            entry = float(getattr(sig["order"], "entry", bar["close"]))
                            stop  = float(sig["bracket"].stop_price)

                            # target: use bracket if present, else compute 1R default
                            target = getattr(sig["bracket"], "target_price", None)
                            if target is None:
                                r_pts = abs(entry - stop)
                                target = entry + (r_pts * sc["target_R"] * (1 if side == "BUY" else -1))

                            out = {
                                "ts": ts_utc.isoformat(),
                                "t_local": bar["t_local"],
                                "reason": "SIGNAL",
                                "side": side,
                                "qty": qty,
                                "entry": round(entry, 2),
                                "stop": round(stop, 2),
                                "target": round(float(target), 2),
                                "atr_mult": sc["atr_mult"],
                                "target_R": sc["target_R"],
                                "break_eps_ticks": sc["break_eps_ticks"],
                                "slope_min": sc.get("slope_min", 0.0),
                                "cooldown_bars": cooldown_bars,
                            }
                            pd.DataFrame([out]).to_csv(args.logfile, mode="a", header=False, index=False)
                            print("[SIGNAL]", out)
                            last_signal_idx = cur_idx

            minute_buf = []  # reset minute buffer

        # push current 5s bar into active minute
        minute_buf.append({
            "datetime": ts_local,
            "open": float(b.open),
            "high": float(b.high),
            "low":  float(b.low),
            "close": float(b.close),
            "volume": float(b.volume or 0),
        })
        last_emitted_min = minute_key

    rt.updateEvent += on_new_5s_bar

    try:
        print("[RUN] Shadow loop started. Press Ctrl+C to stop.")
        while True:
            ib.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[STOP] Exiting…")
    finally:
        ib.disconnect()

if __name__ == "__main__":
    main()
