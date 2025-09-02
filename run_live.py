import asyncio, yaml, pandas as pd, signal, sys, argparse
import os, json, csv
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from contextlib import suppress
from trader.telemetry.metrics import data_age_seconds
# --- make ib_insync play nicely with asyncio ---
try:
    from ib_insync import util as ib_util
    ib_util.patchAsyncio()
except Exception:
    pass

from trader.brokers.ibkr import IbkrBroker
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.engine.backtest import prepare_bars
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
from trader.telemetry.metrics import (
    start_metrics_server, orders_submitted, orders_filled,
    pnl_realized, pnl_unrealized, open_positions, latency_ms
)
from trader.telemetry.execution_log import log_submit, log_fill, log_flatten
from trader.persistence.state_store import StateStore, OpenTradeState, RiskState

class RollingBars:
    def __init__(self, tz: str, orb_minutes: int, symbol: str, max_rows: int = 2000):
        self.df = pd.DataFrame(columns=["datetime","open","high","low","close","volume"])
        self.tz = tz; self.orb_minutes = orb_minutes; self.symbol = symbol; self.max_rows = max_rows
    def add(self, bar_dict):
        self.df.loc[len(self.df)] = bar_dict
        if len(self.df) > self.max_rows:
            self.df = self.df.iloc[-self.max_rows:]
    def features(self):
        if self.df.empty: return None
        tmp = self.df.copy()
        tmp["datetime"] = pd.to_datetime(tmp["datetime"], utc=True)
        tmp["symbol"] = self.symbol
        return prepare_bars(tmp, self.tz, self.orb_minutes)

class TradeTracker:
    def __init__(self): self.reset()
    def set(self, side, qty, stop, target, parent_id, stop_id, target_id, entry_ts):
        self.side = side; self.qty = qty; self.stop = stop; self.target = target
        self.parent_id = parent_id; self.stop_id = stop_id; self.target_id = target_id
        self.entry_ts = entry_ts; self.open = True
    def reset(self):
        self.side=None; self.qty=0; self.stop=None; self.target=None
        self.parent_id=None; self.stop_id=None; self.target_id=None
        self.entry_ts=None; self.open=False
    def classify_fill(self, order_id: int) -> str:
        if order_id == self.stop_id: return "STOP"
        if order_id == self.target_id: return "TARGET"
        return "OTHER"


def _ensure_dir(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)

def write_daily_summary(day_str: str, stats: dict, csv_path="logs/daily_summaries.csv"):
    """
    Append to CSV and also write JSON snapshot for the day.
    """
    _ensure_dir(csv_path)
    # CSV append
    header = ["date","submits","targets","stops","realized_R","watchdog_events","first_signal","last_signal"]
    exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not exists:
            w.writeheader()
        row = {
            "date": day_str,
            "submits": stats.get("submits", 0),
            "targets": stats.get("targets", 0),
            "stops": stats.get("stops", 0),
            "realized_R": round(float(stats.get("realized_R", 0.0)), 4),
            "watchdog_events": stats.get("watchdog_events", 0),
            "first_signal": stats.get("first_signal") or "",
            "last_signal": stats.get("last_signal") or "",
        }
        w.writerow(row)
    # JSON snapshot
    snap_path = f"logs/summary-{day_str}.json"
    _ensure_dir(snap_path)
    with open(snap_path, "w") as f:
        json.dump({"date": day_str, **row}, f, indent=2)

async def main(cfg_path="config/settings.live.yaml", symbol="MES", 
               dry_run=False, dry_run_replay=0, dry_run_ignore_gates=False):
    cfg = yaml.safe_load(open(cfg_path, "r"))
    # --- safety guard: account vs data mode ---
    acct = str(cfg.get("ibkr", {}).get("account", "") or "")
    is_paper = acct.upper().startswith(("D", "DU"))  # paper accounts usually start with D/DU
    use_delayed = bool(cfg.get("ibkr", {}).get("use_delayed", False))

    if use_delayed and not is_paper:
        print(f"[ABORT] use_delayed=true but account looks LIVE: {acct}")
        sys.exit(2)
    allow_mismatch = bool((cfg.get("safety", {}) or {}).get("allow_data_mismatch", False))
    if not use_delayed and is_paper and not allow_mismatch:
        print(f"[ABORT] use_delayed=false (real-time) but account looks PAPER: {acct}")
        sys.exit(2)
    elif not use_delayed and is_paper and allow_mismatch:
        print("[WARN] use_delayed=false on PAPER; proceeding due to allow_data_mismatch=True")

    # (optional) sanity warn on port mismatch (doesn't abort)
    port = int(cfg.get("ibkr", {}).get("port", 0))
    if is_paper and port not in (7497, 4002):
        print(f"[WARN] Paper account {acct} but port={port} (expected 7497 TWS / 4002 IBG)")
    if not is_paper and port not in (7496, 4001):
        print(f"[WARN] Live account {acct} but port={port} (expected 7496 TWS / 4001 IBG)")

    start_metrics_server(cfg["metrics"]["host"], cfg["metrics"]["port"])

    # ----- instrument merge (ES/MES) -----
    contracts = yaml.safe_load(open("config/contracts.yaml", "r"))
    ct = contracts[symbol]

    # ensure fees section exists & hydrate from contract book
    if "fees" not in cfg: cfg["fees"] = {}
    for k in ("tick_size","tick_value","commission_per_contract","exchange_fees_per_contract"):
        if k in ct: cfg["fees"][k] = ct[k]
    fees = cfg["fees"]
    cfg["symbol"] = ct.get("symbol", symbol)

    # --- execution knobs (compute once) ---
    ib_cfg   = cfg.get("ibkr", {})
    exec_cfg = cfg.get("execution", {})
    avoid_mkt = bool(exec_cfg.get("avoid_market_orders", ib_cfg.get("use_delayed", False)))
    slp_ticks = int(exec_cfg.get("limit_slippage_ticks", 2))
    tick_size = float(cfg.get("fees", {}).get("tick_size", 0.0))
    tif         = exec_cfg.get("tif", "GTC")
    outside_rth = bool(exec_cfg.get("outside_rth", True))

    if tick_size <= 0:
        raise ValueError("fees.tick_size missing/invalid")

    def tick_round(px: float) -> float:
        return round(px / tick_size) * tick_size

    # risk / strategy
    risk = RiskGovernor(RiskConfig(
        account_equity=cfg["risk"]["account_equity"], risk_pct=cfg["risk"]["risk_pct"],
        max_daily_loss_R=cfg["risk"]["max_daily_loss_R"], max_consec_losses=cfg["risk"]["max_consec_losses"],
        tick_value=fees["tick_value"], flat_time=cfg["risk"]["flat_time"],
        news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
    ))
    risk.risk_buffer_pct = float(cfg.get("risk", {}).get("risk_buffer_pct", 1.0))
    print(f"[RISK] buffer={getattr(risk,'risk_buffer_pct',1.0):.2f}")
    risk.max_contracts_per_trade = int(cfg["risk"].get("max_contracts_per_trade", 0)) or None
    sc = cfg["strategy"]
    strat = HybridOrbVwap(StratConfig(
        orb_minutes=sc["orb_minutes"], atr_len=sc["atr_len"], break_eps_ticks=sc["break_eps_ticks"],
        atr_mult=sc["atr_mult"], target_R=sc["target_R"], slope_min=sc["slope_min"],
        stop_pad_ticks=sc["stop_pad_ticks"], trail_pad_ticks=sc["trail_pad_ticks"],
        tick_size=fees["tick_size"], tick_value=fees["tick_value"],
    ))
    windows = sc["session_windows"]

    # persistence
    log_path = cfg.get("logging_path", "logs/executions.csv")
    state_path = cfg.get("state_path", "state/bot_state.json")
    store = StateStore(state_path)
    rs = store.load_risk()
    if rs:
        risk.restore({"realized_R": rs.realized_R, "consec_losses": rs.consec_losses, "halted": rs.halted})

    # IBKR connect
    ibc = IbkrBroker(cfg["ibkr"]["host"], cfg["ibkr"]["port"], cfg["ibkr"]["client_id"], cfg["ibkr"]["account"])
    await ibc.connect(readonly=dry_run)

    if cfg["ibkr"].get("use_delayed", False):
        ibc.set_market_data_type(3)  # 3=DELAYED
        print("[INFO] IBKR market data: DELAYED")
    else:
        ibc.set_market_data_type(1)  # 1=REALTIME
        print("[INFO] IBKR market data: REAL-TIME")

    await asyncio.sleep(0.10)
    ibc.enforce_market_data_guard(
        use_delayed=bool(cfg["ibkr"].get("use_delayed", False)),
        allow_mismatch=bool((cfg.get("safety", {}) or {}).get("allow_data_mismatch", False)),
    )
    # Resolve and **set** the contract (use con_id/local_symbol if provided)
    contract = await ibc.resolve_contract(
        cfg["symbol"], cfg["ibkr"]["exchange"], cfg["ibkr"]["currency"],
        cfg["ibkr"]["use_continuous"], cfg["ibkr"]["front_month"],
        con_id=cfg["ibkr"].get("con_id"), local_symbol=cfg["ibkr"].get("local_symbol")
    )
    print(f"[CONTRACT] localSymbol={getattr(contract,'localSymbol','?')} conId={getattr(contract,'conId','?')}")
    ibc.contract = contract  # critical for streamer/orders

    def _expiry_dt(ltm: str):
        if not ltm: return None
        y, m, d = int(ltm[:4]), int(ltm[4:6]), int(ltm[6:8])
        return datetime(y, m, d, tzinfo=timezone.utc)

    roll_warn_days = int(cfg.get("ibkr", {}).get("roll_warn_days", 7))
    expiry = _expiry_dt(getattr(contract, "lastTradeDateOrContractMonth", ""))

    async def roll_watch():
        while True:
            await asyncio.sleep(3600)
            if not expiry: continue
            days = (expiry - datetime.now(timezone.utc)).days
            if days <= roll_warn_days:
                print(f"[ROLL] {cfg['symbol']} {getattr(contract,'localSymbol','?')} expires in {days}d; update conId before that.")
                break
    asyncio.create_task(roll_watch())


    # PnL stream (sync handler)
    def pnl_handler(realized, unrealized):
        pnl_realized.set(float(realized))
        pnl_unrealized.set(float(unrealized))
    try:
        await ibc.start_pnl_stream(cfg["ibkr"].get("account") or None, getattr(contract, "conId", None), pnl_handler)
        print("[INFO] PnL stream started")
    except Exception as e:
        print(f"[WARN] PnL stream unavailable ({e}); continuing without it")

    # Bars stream (delayed path should use historical keepUpToDate in broker)
    rb = RollingBars(cfg["timezone"], sc["orb_minutes"], symbol=cfg["symbol"])
    bar_task = asyncio.create_task(ibc.stream_realtime_bars(
        on_bar=rb.add,
        what_to_show=cfg["ibkr"]["what_to_show"],
        bar_size_secs=cfg["ibkr"]["bar_size_secs"]
    ))

    stop_event = asyncio.Event()



    async def _wait_for_features(rb, *, min_rows=3, timeout=5.0):
        """Wait until rb.features() has at least min_rows, or timeout. Returns DataFrame or None."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while loop.time() - t0 < timeout:
            fdf = rb.features()
            if fdf is not None and not fdf.empty and len(fdf) >= min_rows:
                return fdf
            await asyncio.sleep(0.2)
        return None


    # --- DRY-RUN REPLAY MODE (no IB calls, just prints), then exit ---
    if dry_run and dry_run_replay > 0:
        fdf = await _wait_for_features(rb, min_rows=3, timeout=5.0)
        if fdf is None:
            print("[DRY-RUN] features not ready after 5s; "
                    "ensure this block is AFTER bar_task creation and TWS/HMDS is up")
            return

        # use a clean risk governor so halted/limits don't block replay
        risk_replay = RiskGovernor(RiskConfig(
            account_equity=cfg["risk"]["account_equity"],
            risk_pct=cfg["risk"]["risk_pct"],
            max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
            max_consec_losses=cfg["risk"]["max_consec_losses"],
            tick_value=fees["tick_value"],
            flat_time="23:59",          # keep replay open
            news_lockout_minutes=0,
        ))
        risk_replay.risk_buffer_pct = float(cfg.get("risk", {}).get("risk_buffer_pct", 1.0))
        # honor your YAML clamp
        risk_replay.max_contracts_per_trade = int(cfg["risk"].get("max_contracts_per_trade", 0)) or None

        # locals used for prints / sizing math
        cooldown_bars = int(cfg.get("execution", {}).get("cooldown_bars", 12))
        slp_ticks     = int(cfg.get("execution", {}).get("limit_slippage_ticks", 2))
        tick_size     = float(cfg["fees"]["tick_size"])
        usd_per_point = float(cfg["fees"]["tick_value"]) / tick_size

        n = min(int(dry_run_replay), len(fdf))
        last_sig_idx = -10_000
        prev_date = None

        def _ts_from_row(row):
            # Prefer t_local, fall back to datetime/time-ish keys
            for k in ("t_local", "datetime", "t", "time"):
                if k in row.index and row[k] is not None:
                    return str(row[k])
            return "?"

        row = fdf.iloc[-1]
        ts_full = _ts_from_row(row)
        ts_tail = ts_full[-5:] if isinstance(ts_full, str) and len(ts_full) >= 5 else ts_full

        dt_full = str(row["datetime"]) if "datetime" in row.index else "?"
        dt_tail = dt_full[-5:] if isinstance(dt_full, str) and len(dt_full) >= 5 else dt_full

        print(f"[DRY-RUN] features rows={len(fdf)} (raw rb.df rows={len(rb.df)}) "
            f"last_ts={ts_tail}, datetime={dt_tail}")
        print(f"[DRY-RUN] replaying last {n} bars...")


        count = 0
        for i in range(len(fdf) - n, len(fdf)):
            bar_i = fdf.iloc[i]

            # day reset
            if prev_date is None or bar_i["date"] != prev_date:
                risk_replay.reset_day()
                prev_date = bar_i["date"]

            # optionally honor session window unless --dry-run-ignore-gates
            if not dry_run_ignore_gates and not risk_replay.can_trade_now(bar_i["t_local"]):
                continue

            # cooldown
            if i - last_sig_idx < cooldown_bars:
                continue

            # strat decision
            risk_replay._dbg_ts = _ts_from_row(bar_i)
            sig = strat.maybe_signal(bar_i, windows, risk_replay)
            if not sig:
                ps = getattr(risk_replay, "_last_ps", None)
                ts = _ts_from_row(bar_i)
                # only print if position_size() was actually computed for THIS bar
                if ps and ps.get("ts") == ts and ps.get("qty", 1) == 0:
                    allowed = ps.get("allowed", 0.0) or 0.0
                    rpc = ps.get("risk_per_ct", 0.0) or 0.0
                    ratio = (rpc / allowed) if allowed > 0 else float("inf")
                    print(f"[DRY-RUN] {ts} skipped: qty=0 (risk/ct=${rpc:.2f}, cap=${allowed:.2f}, r={ratio:.2f}x)")
                continue

            # order terms
            qty    = int(sig["order"].qty)
            side   = sig["order"].side
            stop   = float(sig["bracket"].stop_price)
            target = float(sig["bracket"].target_price)

            # price we would try to get with a limit (slippage cushion)
            last_close = float(bar_i["close"])
            dx        = slp_ticks * tick_size
            limit_px  = tick_round(last_close + dx if side.upper() == "BUY" else last_close - dx)
            entry_ref = float(getattr(sig["order"], "entry", bar_i["close"]))

            stop_dist   = abs(float(stop)   - entry_ref)
            target_dist = abs(float(target) - entry_ref)

            if side.upper() == "BUY":
                stop   = tick_round(limit_px - stop_dist)
                target = tick_round(limit_px + target_dist)
            else:  # SELL
                stop   = tick_round(limit_px + stop_dist)
                target = tick_round(limit_px - target_dist)


            # ---- risk math for the print ----
            
            stop_dist_pts     = abs(limit_px - stop)
            risk_per_contract = stop_dist_pts * usd_per_point
            allowed_risk      = float(cfg["risk"]["account_equity"]) * float(cfg["risk"]["risk_pct"]) * float(cfg["risk"].get("risk_buffer_pct", 1.0))
            risk_total        = risk_per_contract * qty
            ratio             = (risk_total / allowed_risk) if allowed_risk > 0 else 0.0

            ts = _ts_from_row(bar_i)
            print(
                f"[DRY-RUN] {ts} would place {side} x{qty} "
                f"limit={limit_px:.2f} stop={stop:.2f} target={target:.2f} "
                f"(risk/ct=${risk_per_contract:.2f}, total=${risk_total:.2f}, cap=${allowed_risk:.2f}, r={ratio:.2f}x)"
            )

            last_sig_idx = i
            count += 1

        print(f"[DRY-RUN] replay complete. signals={count}")
        return






    tracker = TradeTracker()

    # reconcile any saved open trade
    ot = store.load_open_trade()
    if ot:
        positions = await ibc.ib.positionsAsync()
        qty_total = 0
        for p in positions:
            if getattr(p.contract, "conId", None) == getattr(contract, "conId", None):
                qty_total += p.position
        if qty_total != 0:
            tracker.set(ot.side, ot.qty, ot.stop, ot.target, ot.parent_id, ot.stop_id, ot.target_id, ot.entry_ts_utc)
            open_positions.set(1)
        else:
            store.clear_open_trade()

    # exec callback
    def on_exec(trade, fill):
        if not tracker.open: return
        reason = tracker.classify_fill(getattr(trade.order, "orderId", -1))
        if reason in ("STOP","TARGET"):
            orders_filled.labels(side=tracker.side, reason=reason).inc()
            risk.record_trade_outcome_R(1.0 if reason == "TARGET" else -1.0)
            log_fill(
                log_path, symbol=cfg["symbol"], side=tracker.side, qty=tracker.qty,
                entry_price=None, exit_price=None, exit_reason=reason,
                pnl_dollars=None, R=(1.0 if reason=="TARGET" else -1.0),
                order_id=getattr(trade.order, "orderId", None)
            )
            # daily stats
            if reason == "TARGET":
                day_stats["targets"] = day_stats.get("targets", 0) + 1
                day_stats["realized_R"] = day_stats.get("realized_R", 0.0) + 1.0
            elif reason == "STOP":
                day_stats["stops"] = day_stats.get("stops", 0) + 1
                day_stats["realized_R"] = day_stats.get("realized_R", 0.0) - 1.0
            store.clear_open_trade()
            store.save_risk(RiskState(realized_R=risk.realized_R, consec_losses=risk.consec_losses, halted=risk.halted))
            if tracker.entry_ts:
                dt_ms = (datetime.now(timezone.utc) - tracker.entry_ts).total_seconds()*1000
                latency_ms.observe(dt_ms)
            tracker.reset(); open_positions.set(0)

    ibc.on_exec_details(on_exec)

    # ---- Kill-switch / graceful shutdown ----
    watchdog_task = None
    async def shutdown(*_):
        try:
            log_flatten(log_path, symbol=cfg["symbol"], notes="killswitch")
            if hasattr(ibc, "flatten_all"):
                await ibc.flatten_all()
        finally:
            # stop PnL polling/subs before breaking the socket
            try:
                await ibc.stop_pnl_stream()
            except Exception as e:
                print(f"[WARN] stop_pnl_stream: {e}")

            # cancel bar stream task
            try:
                bar_task.cancel()
                with suppress(asyncio.CancelledError):
                    await bar_task
            except Exception:
                pass

            try:
                watchdog_task.cancel()
                with suppress(asyncio.CancelledError):
                    await watchdog_task
            except Exception:
                pass

            store.clear_open_trade()
            store.save_risk(RiskState(realized_R=risk.realized_R, consec_losses=risk.consec_losses, halted=True))

            await ibc.disconnect()
            

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # --- daily stats & EOD summary ---
    day_stats = {"submits":0, "targets":0, "stops":0, "realized_R":0.0,
                "watchdog_events":0, "first_signal":None, "last_signal":None}

    async def watchdog():
        while True:
            await asyncio.sleep(10)
            try:
                await ibc.ensure_connected()
            except Exception as e:
                day_stats["watchdog_events"] = day_stats.get("watchdog_events", 0) + 1
                print(f"[WATCHDOG] {e}")

    watchdog_task = asyncio.create_task(watchdog())

    # ---- live loop ----
    prev_date = None
    max_lag = cfg.get("ibkr", {}).get("max_data_lag_sec", 120)
    if cfg.get("ibkr", {}).get("use_delayed", False) and max_lag < 900:
        max_lag = 1200  # 20 minutes is safe for CME delayed
    print(f"[INFO] data staleness gate = {max_lag}s (delayed={cfg['ibkr'].get('use_delayed', False)})")
    if dry_run:
        print("[DRY-RUN] enabled: no orders will be sent")
    if dry_run and dry_run_ignore_gates:
        print("[DRY-RUN] ignoring staleness + session windows")
    heartbeat_next = 0.0

    # --- de-dupe & cooldown state ---
    last_signal_dt_iso = None
    last_sig_idx = -10_000
    cooldown_bars = int(cfg.get("execution", {}).get("cooldown_bars", 12))  # default 12 bars

    
    tz_local = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    flat_h, flat_m = map(int, str(cfg["risk"]["flat_time"]).split(":"))
    summary_emitted_for = None  # date string we already wrote summary for



    # before the while loop
    last_signal_dt_iso = None
    last_sig_idx = -10_000
    cooldown_bars = int(cfg.get("execution", {}).get("cooldown_bars", 12))  # default 12

    
    while not stop_event.is_set():
        await asyncio.sleep(0.3)
    
        # features + staleness gate (critical for delayed data)
        fdf = rb.features()
        if fdf is None or fdf.empty:
            continue

        # age of the latest raw bar pushed in rb.df
        try:
            last_dt = pd.to_datetime(rb.df.iloc[-1]["datetime"], utc=True)
            age_sec = (datetime.now(timezone.utc) - last_dt).total_seconds()
            data_age_seconds.set(age_sec)
            now_ts = datetime.now(timezone.utc).timestamp()
            if now_ts >= heartbeat_next:
                print(f"[HEARTBEAT] last_bar={last_dt.isoformat()} age={age_sec:.1f}s max_lag={max_lag}")
                heartbeat_next = now_ts + 30  # every 30s

            if not (dry_run and dry_run_ignore_gates) and age_sec > max_lag:
                continue  # too stale -> skip signals/orders
        except Exception:
            pass

        bar = fdf.iloc[-1]

        # --- EOD summary trigger (once per day) ---
        now_loc = datetime.now(tz_local)
        today_str = now_loc.date().isoformat()
        if today_str != summary_emitted_for and (now_loc.hour, now_loc.minute) >= (flat_h, flat_m):
            write_daily_summary(prev_date or today_str, day_stats)
            summary_emitted_for = today_str

        bar_ts_iso = str(rb.df.iloc[-1]["datetime"])
        if bar_ts_iso == last_signal_dt_iso:
            continue
        # day reset
        if prev_date is None or bar["date"] != prev_date:
            if prev_date is not None and summary_emitted_for != prev_date:
                write_daily_summary(prev_date, day_stats)
                summary_emitted_for = prev_date
        # reset for new day
            day_stats = {"submits":0, "targets":0, "stops":0, "realized_R":0.0,
                        "watchdog_events":0, "first_signal":None, "last_signal":None}
            risk.reset_day(); prev_date = bar["date"]
            store.save_risk(RiskState(realized_R=risk.realized_R, consec_losses=risk.consec_losses, halted=risk.halted))

        if not (dry_run and dry_run_ignore_gates) and not risk.can_trade_now(bar["t_local"]):
            continue

        if not tracker.open and not risk.halted:
            sig = strat.maybe_signal(bar, windows, risk)
            if not sig:
                continue
                
            if (len(fdf) - 1) - last_sig_idx < cooldown_bars:
                continue

            qty = sig["order"].qty
            side = sig["order"].side
            stop = float(sig["bracket"].stop_price)
            target = float(sig["bracket"].target_price)
            t0 = datetime.now(timezone.utc)

            # === ENTRY PLACEMENT (limit on delayed; market otherwise) ===
            last_close = float(bar["close"])
            dx = slp_ticks * tick_size
            limit_px = tick_round(last_close + dx if side.upper() == "BUY" else last_close - dx)
            entry_ref = float(getattr(sig["order"], "entry", bar["close"]))

            stop_dist   = abs(float(stop)   - entry_ref)
            target_dist = abs(float(target) - entry_ref)

            if side.upper() == "BUY":
                stop   = tick_round(limit_px - stop_dist)
                target = tick_round(limit_px + target_dist)
            else:  # SELL
                stop   = tick_round(limit_px + stop_dist)
                target = tick_round(limit_px - target_dist)


            if dry_run:
                # DRY RUN: only log what would be sent, don't touch IB or state
                ts = str(bar.get("t_local", rb.df.iloc[-1]["datetime"]))

                # risk math
                usd_per_point   = float(cfg["fees"]["tick_value"]) / float(cfg["fees"]["tick_size"])
                stop_dist_pts   = abs(limit_px - stop)
                risk_per_contract = stop_dist_pts * usd_per_point
                allowed_risk    = (float(cfg["risk"]["account_equity"]) *
                                float(cfg["risk"]["risk_pct"]) *
                                float(cfg["risk"].get("risk_buffer_pct", 1.0)))
                risk_total      = risk_per_contract * int(qty)
                ratio           = (risk_total / allowed_risk) if allowed_risk > 0 else 0.0
                print(
                    f"[DRY-RUN] {ts} would place {side} x{qty} "
                    f"limit={limit_px:.2f} stop={stop:.2f} target={target:.2f} "
                    f"(risk/ct=${risk_per_contract:.2f}, total=${risk_total:.2f}, cap=${allowed_risk:.2f}, r={ratio:.2f}x)"
                )
                # daily stats timestamps (first/last "signal" seen)
                ts_local = str(bar.get("t_local", rb.df.iloc[-1]["datetime"]))
                if day_stats["first_signal"] is None:
                    day_stats["first_signal"] = ts_local
                day_stats["last_signal"] = ts_local
                # do NOT modify tracker/open_positions/store/risk in dry-run
            else:
                use_limit = avoid_mkt and hasattr(ibc, "place_bracket_limit")
                try:
                    if use_limit:
                        trades = await ibc.place_bracket_limit(side, qty, limit_px, stop, target,
                                                            tif=tif, outsideRth=outside_rth)
                    else:
                        trades = await ibc.place_bracket_market(side, qty, stop, target,
                                                                tif=tif, outsideRth=outside_rth)
                except Exception as e:
                    print(f"[WARN] place_bracket_limit failed ({e}); falling back to market")
                    trades = await ibc.place_bracket_market(side, qty, stop, target,
                                                            tif=tif, outsideRth=outside_rth)

                parent_id = trades[0].order.orderId; tp_id = trades[1].order.orderId; sl_id = trades[2].order.orderId
                orders_submitted.labels(side=side).inc(); open_positions.set(1)
                tracker.set(side, qty, stop, target, parent_id, sl_id, tp_id, t0)

                # daily stats (real submits only)
                if day_stats["first_signal"] is None:
                    day_stats["first_signal"] = str(bar.get("t_local", rb.df.iloc[-1]["datetime"]))
                day_stats["last_signal"] = str(bar.get("t_local", rb.df.iloc[-1]["datetime"]))
                day_stats["submits"] = day_stats.get("submits", 0) + 1

                store.save_open_trade(OpenTradeState(
                    symbol=cfg["symbol"], side=side, qty=qty, stop=stop, target=target,
                    parent_id=parent_id, stop_id=sl_id, target_id=tp_id, entry_ts_utc=t0.isoformat()
                ))
                log_submit(log_path, symbol=cfg["symbol"], side=side, qty=qty, stop=stop, target=target,
                        parent_id=parent_id, tp_id=tp_id, sl_id=sl_id)
                store.save_risk(RiskState(realized_R=risk.realized_R, consec_losses=risk.consec_losses, halted=risk.halted))

            # remember we acted on this bar (even in dry-run)
        last_signal_dt_iso = bar_ts_iso
        last_sig_idx = len(fdf) - 1
    await shutdown()
    return 

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.dev.yaml", help="Path to environment YAML")
    ap.add_argument("--symbol", default="MES", choices=["ES","MES"], help="Instrument to use from contracts.yaml")
    ap.add_argument("--dry-run", action="store_true", help="Log orders that would be placed; do not touch IB/account state")
    ap.add_argument("--dry-run-replay", type=int, default=0,
                help="In dry-run, replay last N bars and print 'would place' lines, then exit")
    ap.add_argument("--dry-run-ignore-gates", action="store_true",
                help="In dry-run, ignore staleness and session windows")
    args = ap.parse_args()
    asyncio.run(
        main(
            cfg_path=args.config, 
            symbol=args.symbol, 
            dry_run=args.dry_run,
            dry_run_replay=args.dry_run_replay,
            dry_run_ignore_gates=args.dry_run_ignore_gates,
        )
    )

