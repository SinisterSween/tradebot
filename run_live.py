import asyncio, yaml, pandas as pd, signal, sys, argparse
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

async def main(cfg_path="config/settings.live.yaml", symbol="MES"):
    cfg = yaml.safe_load(open(cfg_path, "r"))
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
    await ibc.connect()
    if cfg["ibkr"].get("use_delayed", False):
        ibc.set_market_data_type(3)  # 3=DELAYED
        print("[INFO] IBKR market data: DELAYED")
    else:
        print("[INFO] IBKR market data: REAL-TIME")

    # Resolve and **set** the contract (use con_id/local_symbol if provided)
    contract = await ibc.resolve_contract(
        cfg["symbol"], cfg["ibkr"]["exchange"], cfg["ibkr"]["currency"],
        cfg["ibkr"]["use_continuous"], cfg["ibkr"]["front_month"],
        con_id=cfg["ibkr"].get("con_id"), local_symbol=cfg["ibkr"].get("local_symbol")
    )
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
            sys.exit(0)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s)))

    async def watchdog():
        while True:
            await asyncio.sleep(10)
            try:
                await ibc.ensure_connected()
            except Exception as e:
                print(f"[WATCHDOG] {e}")
    watchdog_task = asyncio.create_task(watchdog())

    # ---- live loop ----
    prev_date = None
    max_lag = cfg.get("ibkr", {}).get("max_data_lag_sec", 120)
    if cfg.get("ibkr", {}).get("use_delayed", False) and max_lag < 900:
        max_lag = 1200  # 20 minutes is safe for CME delayed
    print(f"[INFO] data staleness gate = {max_lag}s (delayed={cfg['ibkr'].get('use_delayed', False)})")
    heartbeat_next = 0.0
    # before the while loop
    last_signal_dt_iso = None
    last_sig_idx = -10_000
    cooldown_bars = int(cfg.get("execution", {}).get("cooldown_bars", 12))  # default 12

    while True:
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

            if age_sec > max_lag:
                continue  # too stale -> skip signals/orders
        except Exception:
            pass

        bar = fdf.iloc[-1]
        bar_ts_iso = str(rb.df.iloc[-1]["datetime"])
        if bar_ts_iso == last_signal_dt_iso:
            continue
        # day reset
        if prev_date is None or bar["date"] != prev_date:
            risk.reset_day(); prev_date = bar["date"]
            store.save_risk(RiskState(realized_R=risk.realized_R, consec_losses=risk.consec_losses, halted=risk.halted))

        if not risk.can_trade_now(bar["t_local"]):
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

            use_limit = avoid_mkt and hasattr(ibc, "place_bracket_limit")
            try:
                if use_limit:
                    trades = await ibc.place_bracket_limit(side, qty, limit_px, stop, target)
                else:
                    trades = await ibc.place_bracket_market(side, qty, stop, target)
            except Exception as e:
                print(f"[WARN] place_bracket_limit failed ({e}); falling back to market")
                trades = await ibc.place_bracket_market(side, qty, stop, target)

            parent_id = trades[0].order.orderId; tp_id = trades[1].order.orderId; sl_id = trades[2].order.orderId
            orders_submitted.labels(side=side).inc(); open_positions.set(1)
            tracker.set(side, qty, stop, target, parent_id, sl_id, tp_id, t0)

            store.save_open_trade(OpenTradeState(
                symbol=cfg["symbol"], side=side, qty=qty, stop=stop, target=target,
                parent_id=parent_id, stop_id=sl_id, target_id=tp_id, entry_ts_utc=t0.isoformat()
            ))
            log_submit(log_path, symbol=cfg["symbol"], side=side, qty=qty, stop=stop, target=target,
                       parent_id=parent_id, tp_id=tp_id, sl_id=sl_id)
            store.save_risk(RiskState(realized_R=risk.realized_R, consec_losses=risk.consec_losses, halted=risk.halted))
            last_signal_dt_iso = bar_ts_iso
            last_sig_idx = len(fdf) - 1
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.dev.yaml", help="Path to environment YAML")
    ap.add_argument("--symbol", default="MES", choices=["ES","MES"], help="Instrument to use from contracts.yaml")
    args = ap.parse_args()
    asyncio.run(main(cfg_path=args.config, symbol=args.symbol))
