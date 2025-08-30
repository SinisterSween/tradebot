import asyncio, yaml, pandas as pd, signal, sys, argparse
from datetime import datetime, timezone

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

    contract = await ibc.resolve_contract(
        cfg["symbol"], cfg["ibkr"]["exchange"], cfg["ibkr"]["currency"],
        cfg["ibkr"]["use_continuous"], cfg["ibkr"]["front_month"]
    )

    # PnL stream
    async def pnl_handler(realized, unrealized):
        pnl_realized.set(float(realized)); pnl_unrealized.set(float(unrealized))
    await ibc.start_pnl_stream(cfg["ibkr"].get("account") or None, getattr(contract, "conId", None), pnl_handler)

    # Bars stream (your broker should already do keepUpToDate when delayed)
    rb = RollingBars(cfg["timezone"], sc["orb_minutes"], symbol=cfg["symbol"])
    asyncio.create_task(ibc.stream_realtime_bars(
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

    # Kill-switch
    async def shutdown(*_):
        try:
            log_flatten(log_path, symbol=cfg["symbol"], notes="killswitch")
            await ibc.flatten_all()
        finally:
            store.clear_open_trade()
            store.save_risk(RiskState(realized_R=risk.realized_R, consec_losses=risk.consec_losses, halted=True))
            await ibc.disconnect(); sys.exit(0)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s)))

    # ---- live loop ----
    prev_date = None
    max_lag = cfg.get("ibkr", {}).get("max_data_lag_sec", 120)
    avoid_mkt = cfg.get("execution", {}).get("avoid_market_orders", cfg["ibkr"].get("use_delayed", False))
    slp_ticks = cfg.get("execution", {}).get("limit_slippage_ticks", 2)
    tick_size = float(fees["tick_size"])

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
            if age_sec > max_lag:
                # too stale -> don’t generate signals / place orders
                continue
        except Exception:
            pass

        bar = fdf.iloc[-1]

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

            qty = sig["order"].qty
            side = sig["order"].side
            stop = float(sig["bracket"].stop_price)
            target = float(sig["bracket"].target_price)
            t0 = datetime.now(timezone.utc)

            # Prefer limit bracket when delayed (or explicitly configured)
            trades = None
            if avoid_mkt and hasattr(ibc, "place_bracket_limit"):
                last_close = float(bar["close"])
                if side.upper() == "BUY":
                    limit_px = last_close + slp_ticks * tick_size
                else:
                    limit_px = last_close - slp_ticks * tick_size
                try:
                    trades = await ibc.place_bracket_limit(side, qty, limit_px, stop, target)
                except Exception:
                    # fallback to market if your broker wrapper doesn’t support limit bracket
                    trades = await ibc.place_bracket_market(side, qty, stop, target)
            else:
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

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.dev.yaml", help="Path to environment YAML")
    ap.add_argument("--symbol", default="MES", choices=["ES","MES"], help="Instrument to use from contracts.yaml")
    args = ap.parse_args()
    asyncio.run(main(cfg_path=args.config, symbol=args.symbol))
