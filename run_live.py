import asyncio, yaml, pandas as pd, signal, sys, argparse
import os, json, csv
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from contextlib import suppress
from trader.telemetry.metrics import data_age_seconds
import asyncio, yaml, pandas as pd, signal, sys, argparse, os, json, csv
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from contextlib import suppress
from typing import List, Dict, Any
from dataclasses import dataclass, asdict
from pathlib import Path

# --- make ib_insync play nicely with asyncio ---
try:
    from ib_insync import util as ib_util

    ib_util.patchAsyncio()
except Exception:
    pass

from trader.brokers.ibkr import IbkrBroker
from trader.brokers.ibkr_fractional import IbkrFractional

try:
    from trader.brokers.ccxt_binanceus import CcxtBinanceus
except ModuleNotFoundError as e:
    print(f"[WARN] ccxt/crypto broker unavailable: {e}")
    CcxtBinanceus = None
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.engine.backtest import prepare_bars
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
from trader.telemetry.metrics import (
    start_metrics_server,
    orders_submitted,
    orders_filled,
    pnl_realized,
    pnl_unrealized,
    open_positions,
    latency_ms,
)
from trader.telemetry.execution_log import log_submit, log_fill, log_flatten
from trader.persistence.state_store import StateStore, OpenTradeState, RiskState


class RollingBars:
    def __init__(self, tz: str, orb_minutes: int, symbol: str, max_rows: int = 2000):
        self.df = pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )
        self.tz = tz
        self.orb_minutes = orb_minutes
        self.symbol = symbol
        self.max_rows = max_rows

    def add(self, bar_dict):
        self.df.loc[len(self.df)] = bar_dict
        if len(self.df) > self.max_rows:
            self.df = self.df.iloc[-self.max_rows :]

    def features(self):
        if self.df.empty:
            return None
        tmp = self.df.copy()
        tmp["datetime"] = pd.to_datetime(tmp["datetime"], utc=True)
        tmp["symbol"] = self.symbol
        return prepare_bars(tmp, self.tz, self.orb_minutes)


class TradeTracker:
    def __init__(self):
        self.reset()

    def set(self, side, qty, stop, target, parent_id, stop_id, target_id, entry_ts):
        self.side = side
        self.qty = qty
        self.stop = stop
        self.target = target
        self.parent_id = parent_id
        self.stop_id = stop_id
        self.target_id = target_id
        self.entry_ts = entry_ts
        self.open = True

    def reset(self):
        self.side = None
        self.qty = 0
        self.stop = None
        self.target = None
        self.parent_id = None
        self.stop_id = None
        self.target_id = None
        self.entry_ts = None
        self.open = False

    def classify_fill(self, order_id: int) -> str:
        if order_id == self.stop_id:
            return "STOP"
        if order_id == self.target_id:
            return "TARGET"
        return "OTHER"


def _norm_windows(windows):
    out = []
    for w in windows or []:
        if isinstance(w, dict):
            out.append({"start": str(w.get("start")), "end": str(w.get("end"))})
        elif isinstance(w, (list, tuple)) and len(w) == 2:
            out.append({"start": str(w[0]), "end": str(w[1])})
        else:
            raise ValueError(f"Bad session_windows entry: {w!r}")
    return out


def _ensure_dir(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)


def write_daily_summary(day_str: str, stats: dict, csv_path="logs/daily_summaries.csv"):
    """
    Append to CSV and also write JSON snapshot for the day.
    """
    _ensure_dir(csv_path)
    # CSV append
    header = [
        "date",
        "submits",
        "targets",
        "stops",
        "realized_R",
        "watchdog_events",
        "first_signal",
        "last_signal",
    ]
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


def _deep_merge(a, b):
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def _expected_profile_for_symbol(sym: str) -> str:
    s = (sym or "").upper()
    FUT = {
        "ES",
        "MES",
        "NQ",
        "MNQ",
        "YM",
        "MYM",
        "RTY",
        "M2K",
        "CL",
        "NG",
        "GC",
        "SI",
        "ZB",
        "ZN",
        "ZF",
        "ZT",
        "6E",
        "6B",
        "6J",
    }
    return "futures" if s in FUT else "equities"


# ----------------------------------------------------------------------
# Microlot state persistence (JSON per microlot under state/)
# ----------------------------------------------------------------------
@dataclass
class MicrolotState:
    name: str
    universe: str
    broker: str
    symbol: str | None = None

    cash_alloc: float = 0.0
    max_risk_per_trade: float = 0.0

    trade_count: int = 0

    last_heartbeat_ts: str | None = None
    last_decision_ts: str | None = None
    last_side: str | None = None
    last_qty: float | None = None
    last_stop: float | None = None
    last_target: float | None = None

    realized_R_today: float = 0.0
    realized_pnl: float = 0.0

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        name: str,
        universe: str,
        broker: str,
        cash_alloc: float,
        max_risk_per_trade: float,
    ) -> "MicrolotState":
        """
        Load an existing state file if present; otherwise create a fresh one
        with basic microlot metadata. Ignores unknown keys in old JSON.
        """
        if path.exists():
            with path.open("r") as f:
                data = json.load(f)
            # Filter unknown keys so we don't blow up if the JSON has extras
            field_names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
            filtered = {k: v for k, v in data.items() if k in field_names}
            # Ensure core identity fields are correct
            filtered.setdefault("name", name)
            filtered.setdefault("universe", universe)
            filtered.setdefault("broker", broker)
            filtered.setdefault("cash_alloc", cash_alloc)
            filtered.setdefault("max_risk_per_trade", max_risk_per_trade)
            return cls(**filtered)

        return cls(
            name=name,
            universe=universe,
            broker=broker,
            symbol=None,
            cash_alloc=cash_alloc,
            max_risk_per_trade=max_risk_per_trade,
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(asdict(self), f, indent=2)


async def main(*, args):
    if args.portfolio:
        return await run_portfolio(args)
    # after cfg is built and before the rest of your logic
    symbol = getattr(args, "symbol", "MES")

    # alias CLI flags into locals so existing code works
    dry_run = bool(getattr(args, "dry_run", False))
    dry_run_replay = int(getattr(args, "dry_run_replay", 0) or 0)
    dry_run_ignore_gates = bool(getattr(args, "dry_run_ignore_gates", False))

    cfg_files = []

    # env (highest-priority choice)
    if getattr(args, "env", None):
        cfg_files.append(f"config/env/{args.env}.yaml")

    # profile (next)
    if getattr(args, "profile", None):
        cfg_files.append(f"config/profile/{args.profile}.yaml")

    # Allow explicit legacy --config if you pass it
    if getattr(args, "config", None):
        cfg_files.append(args.config)

    # optional extra (last override)
    if getattr(args, "config_extra", None):
        cfg_files.append(args.config_extra)

    if not cfg_files:
        raise SystemExit(
            "No config specified. Use --env <paper|live> and --profile <equities|futures> "
            "or pass --config <path>."
        )

    cfg = {}
    loaded = []
    for p in cfg_files:
        try:
            with open(p, "r") as f:
                part = yaml.safe_load(f) or {}
            _deep_merge(cfg, part)
            loaded.append(p)
        except FileNotFoundError:
            print(f"[WARN] config file not found: {p}")

    print(f"[CFG] loaded: {', '.join(loaded) if loaded else '<none>'}")

    cfg.setdefault("metrics", {"host": "127.0.0.1", "port": 9100})
    cfg.setdefault("fees", {})
    cfg.setdefault("ibkr", {})
    cfg.setdefault("risk", {})
    cfg.setdefault("execution", {})
    cfg.setdefault("safety", {})

    # profile-vs-symbol sanity
    symbol = getattr(args, "symbol", "MES")
    exp = _expected_profile_for_symbol(symbol)
    sel = getattr(args, "profile", None) or exp
    if sel != exp:
        msg = f"[{'ABORT' if getattr(args, 'strict_profile', False) else 'WARN'}] symbol={symbol} looks like {exp} but profile={sel}"
        print(msg)
        if getattr(args, "strict_profile", False):
            sys.exit(2)

    # --- safety guard: account vs data mode ---
    acct = str(cfg.get("ibkr", {}).get("account", "") or "")
    is_paper = acct.upper().startswith(
        ("D", "DU")
    )  # paper accounts usually start with D/DU
    use_delayed = bool(cfg.get("ibkr", {}).get("use_delayed", False))

    if use_delayed and not is_paper:
        print(f"[ABORT] use_delayed=true but account looks LIVE: {acct}")
        sys.exit(2)
    allow_mismatch = bool(
        (cfg.get("safety", {}) or {}).get("allow_data_mismatch", False)
    )
    if not use_delayed and is_paper and not allow_mismatch:
        print(f"[ABORT] use_delayed=false (real-time) but account looks PAPER: {acct}")
        sys.exit(2)
    elif not use_delayed and is_paper and allow_mismatch:
        print(
            "[WARN] use_delayed=false on PAPER; proceeding due to allow_data_mismatch=True"
        )

    # (optional) sanity warn on port mismatch (doesn't abort)
    port = int(cfg.get("ibkr", {}).get("port", 0))
    if is_paper and port not in (7497, 4002):
        print(
            f"[WARN] Paper account {acct} but port={port} (expected 7497 TWS / 4002 IBG)"
        )
    if not is_paper and port not in (7496, 4001):
        print(
            f"[WARN] Live account {acct} but port={port} (expected 7496 TWS / 4001 IBG)"
        )

    mcfg = cfg.get("metrics", {"host": "127.0.0.1", "port": 9100})
    start_metrics_server(mcfg["host"], mcfg["port"])

    # ----- instrument merge (ES/MES) -----
    sym_u = (symbol or "").upper()
    is_fut = _expected_profile_for_symbol(sym_u) == "futures"

    ct = {}
    if is_fut:
        contracts = yaml.safe_load(open("config/contracts.yaml", "r")) or {}
        if sym_u not in contracts:
            raise KeyError(f"{sym_u} not found in config/contracts.yaml (futures book)")
        ct = contracts[sym_u]

        cfg.setdefault("fees", {})
        for k in ("tick_size","tick_value","commission_per_contract","exchange_fees_per_contract"):
            if k in ct:
                cfg["fees"][k] = ct[k]

    cfg["symbol"] = ct.get("symbol", sym_u)


    # ensure fees section exists & hydrate from contract book
    if "fees" not in cfg:
        cfg["fees"] = {}
    for k in (
        "tick_size",
        "tick_value",
        "commission_per_contract",
        "exchange_fees_per_contract",
    ):
        if k in ct:
            cfg["fees"][k] = ct[k]
    fees = cfg["fees"]
    cfg["symbol"] = ct.get("symbol", symbol)

    # --- execution knobs (compute once) ---
    ib_cfg = cfg.get("ibkr", {})
    exec_cfg = cfg.get("execution", {})
    avoid_mkt = bool(
        exec_cfg.get("avoid_market_orders", ib_cfg.get("use_delayed", False))
    )
    slp_ticks = int(exec_cfg.get("limit_slippage_ticks", 2))
    tick_size = float(cfg.get("fees", {}).get("tick_size", 0.0))
    tif = exec_cfg.get("tif", "GTC")
    outside_rth = bool(exec_cfg.get("outside_rth", True))

    if tick_size <= 0:
        raise ValueError("fees.tick_size missing/invalid")

    def tick_round(px: float) -> float:
        return round(px / tick_size) * tick_size

    # risk / strategy
    risk = RiskGovernor(
        RiskConfig(
            account_equity=cfg["risk"]["account_equity"],
            risk_pct=cfg["risk"]["risk_pct"],
            max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
            max_consec_losses=cfg["risk"]["max_consec_losses"],
            tick_value=fees["tick_value"],
            flat_time=cfg["risk"]["flat_time"],
            news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
        )
    )
    risk.risk_buffer_pct = float(cfg.get("risk", {}).get("risk_buffer_pct", 1.0))
    print(f"[RISK] buffer={getattr(risk, 'risk_buffer_pct', 1.0):.2f}")
    risk.max_contracts_per_trade = (
        int(cfg["risk"].get("max_contracts_per_trade", 0)) or None
    )
    sc = cfg["strategy"]
    strat = HybridOrbVwap(
        StratConfig(
            orb_minutes=sc["orb_minutes"],
            atr_len=sc["atr_len"],
            break_eps_ticks=sc["break_eps_ticks"],
            atr_mult=sc["atr_mult"],
            target_R=sc["target_R"],
            slope_min=sc["slope_min"],
            stop_pad_ticks=sc["stop_pad_ticks"],
            trail_pad_ticks=sc["trail_pad_ticks"],
            tick_size=fees["tick_size"],
            tick_value=fees["tick_value"],
        )
    )
    windows = _norm_windows(sc.get("session_windows", []))

    # persistence
    log_path = cfg.get("logging_path", "logs/executions.csv")
    state_path = cfg.get("state_path", "state/bot_state.json")
    store = StateStore(state_path)
    rs = store.load_risk()
    if rs:
        risk.restore(
            {
                "realized_R": rs.realized_R,
                "consec_losses": rs.consec_losses,
                "halted": rs.halted,
            }
        )

    # IBKR connect
    ibc = IbkrBroker(
        cfg["ibkr"]["host"],
        cfg["ibkr"]["port"],
        cfg["ibkr"]["client_id"],
        cfg["ibkr"]["account"],
    )
    await ibc.connect()
    ibc.use_rtb_for_equities = bool(
        cfg.get("ibkr", {}).get("use_rtb_for_equities", False)
    )
    print(
        f"[STREAM] equities mode: {'RTB 5s->60s' if ibc.use_rtb_for_equities else 'Ticks->60s'}"
    )

    if cfg["ibkr"].get("use_delayed", False):
        ibc.set_market_data_type(3)  # 3=DELAYED
        print("[INFO] IBKR market data: DELAYED")
    else:
        ibc.set_market_data_type(1)  # 1=REALTIME
        print("[INFO] IBKR market data: REAL-TIME")

    await asyncio.sleep(0.10)
    ibc.enforce_market_data_guard(
        use_delayed=bool(cfg["ibkr"].get("use_delayed", False)),
        allow_mismatch=bool(
            (cfg.get("safety", {}) or {}).get("allow_data_mismatch", False)
        ),
    )
    # Resolve and **set** the contract (use con_id/local_symbol if provided)
    ct_exch = ct.get("exchange")
    ct_curr = ct.get("currency")

    ib_exch = cfg.get("ibkr", {}).get("exchange", ct_exch or "SMART")
    ib_curr = cfg.get("ibkr", {}).get("currency", ct_curr or "USD")
    use_cont = bool(cfg.get("ibkr", {}).get("use_continuous", False))
    front_month = cfg.get("ibkr", {}).get("front_month", None)

    contract = await ibc.resolve_contract(
        cfg["symbol"],
        ib_exch,
        ib_curr,
        use_cont,
        front_month,
        con_id=cfg["ibkr"].get("con_id"),
        local_symbol=cfg["ibkr"].get("local_symbol"),
    )
    print(
        f"[CONTRACT] localSymbol={getattr(contract, 'localSymbol', '?')} conId={getattr(contract, 'conId', '?')}"
    )
    ibc.contract = contract  # critical for streamer/orders
    print(
        f"[DBG] secType={getattr(contract, 'secType', '?')} "
        f"exch={getattr(contract, 'exchange', '?')} "
        f"primary={getattr(contract, 'primaryExchange', '?')} "
        f"conId={getattr(contract, 'conId', '?')}"
    )

    def _expiry_dt(ltm: str):
        if not ltm:
            return None
        y, m, d = int(ltm[:4]), int(ltm[4:6]), int(ltm[6:8])
        return datetime(y, m, d, tzinfo=timezone.utc)

    roll_warn_days = int(cfg.get("ibkr", {}).get("roll_warn_days", 7))
    expiry = _expiry_dt(getattr(contract, "lastTradeDateOrContractMonth", ""))

    async def roll_watch():
        while True:
            await asyncio.sleep(3600)
            if not expiry:
                continue
            days = (expiry - datetime.now(timezone.utc)).days
            if days <= roll_warn_days:
                print(
                    f"[ROLL] {cfg['symbol']} {getattr(contract, 'localSymbol', '?')} expires in {days}d; update conId before that."
                )
                break

    asyncio.create_task(roll_watch())

    # PnL stream (sync handler)
    def pnl_handler(realized, unrealized):
        pnl_realized.set(float(realized))
        pnl_unrealized.set(float(unrealized))

    try:
        await ibc.start_pnl_stream(
            cfg["ibkr"].get("account") or None,
            getattr(contract, "conId", None),
            pnl_handler,
        )
        print("[INFO] PnL stream started")
    except Exception as e:
        print(f"[WARN] PnL stream unavailable ({e}); continuing without it")

    # Bars stream (delayed path should use historical keepUpToDate in broker)
    rb = RollingBars(cfg["timezone"], sc["orb_minutes"], symbol=cfg["symbol"])
    bar_task = asyncio.create_task(
        ibc.stream_realtime_bars(
            on_bar=rb.add,
            what_to_show=cfg["ibkr"]["what_to_show"],
            bar_size_secs=cfg["ibkr"]["bar_size_secs"],
        )
    )

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
            print(
                "[DRY-RUN] features not ready after 5s; "
                "ensure this block is AFTER bar_task creation and TWS/HMDS is up"
            )
            return

        # use a clean risk governor so halted/limits don't block replay
        risk_replay = RiskGovernor(
            RiskConfig(
                account_equity=cfg["risk"]["account_equity"],
                risk_pct=cfg["risk"]["risk_pct"],
                max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
                max_consec_losses=cfg["risk"]["max_consec_losses"],
                tick_value=fees["tick_value"],
                flat_time="23:59",  # keep replay open
                news_lockout_minutes=0,
            )
        )
        risk_replay.risk_buffer_pct = float(
            cfg.get("risk", {}).get("risk_buffer_pct", 1.0)
        )
        # honor your YAML clamp
        risk_replay.max_contracts_per_trade = (
            int(cfg["risk"].get("max_contracts_per_trade", 0)) or None
        )

        # locals used for prints / sizing math
        cooldown_bars = int(cfg.get("execution", {}).get("cooldown_bars", 12))
        slp_ticks = int(cfg.get("execution", {}).get("limit_slippage_ticks", 2))
        tick_size = float(cfg["fees"]["tick_size"])
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
        ts_tail = (
            ts_full[-5:] if isinstance(ts_full, str) and len(ts_full) >= 5 else ts_full
        )

        dt_full = str(row["datetime"]) if "datetime" in row.index else "?"
        dt_tail = (
            dt_full[-5:] if isinstance(dt_full, str) and len(dt_full) >= 5 else dt_full
        )

        print(
            f"[DRY-RUN] features rows={len(fdf)} (raw rb.df rows={len(rb.df)}) "
            f"last_ts={ts_tail}, datetime={dt_tail}"
        )
        print(f"[DRY-RUN] replaying last {n} bars...")

        count = 0
        for i in range(len(fdf) - n, len(fdf)):
            bar_i = fdf.iloc[i]

            # day reset
            if prev_date is None or bar_i["date"] != prev_date:
                risk_replay.reset_day()
                prev_date = bar_i["date"]

            # optionally honor session window unless --dry-run-ignore-gates
            ap.add_argument(
                "--portfolio",
                default=None,
                help="Run a multi-asset micro-lot portfolio (config/portfolios/<name>.yaml)",
            )
            if not dry_run_ignore_gates and not risk_replay.can_trade_now(
                bar_i["t_local"]
            ):
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
                    print(
                        f"[DRY-RUN] {ts} skipped: qty=0 (risk/ct=${rpc:.2f}, cap=${allowed:.2f}, r={ratio:.2f}x)"
                    )
                continue

            # order terms
            qty = int(sig["order"].qty)
            side = sig["order"].side
            stop = float(sig["bracket"].stop_price)
            target = float(sig["bracket"].target_price)

            # price we would try to get with a limit (slippage cushion)
            last_close = float(bar_i["close"])
            dx = slp_ticks * tick_size
            limit_px = tick_round(
                last_close + dx if side.upper() == "BUY" else last_close - dx
            )
            entry_ref = float(getattr(sig["order"], "entry", bar_i["close"]))

            stop_dist = abs(float(stop) - entry_ref)
            target_dist = abs(float(target) - entry_ref)

            if side.upper() == "BUY":
                stop = tick_round(limit_px - stop_dist)
                target = tick_round(limit_px + target_dist)
            else:  # SELL
                stop = tick_round(limit_px + stop_dist)
                target = tick_round(limit_px - target_dist)

            # ---- risk math for the print ----

            stop_dist_pts = abs(limit_px - stop)
            risk_per_contract = stop_dist_pts * usd_per_point
            allowed_risk = (
                float(cfg["risk"]["account_equity"])
                * float(cfg["risk"]["risk_pct"])
                * float(cfg["risk"].get("risk_buffer_pct", 1.0))
            )
            risk_total = risk_per_contract * qty
            ratio = (risk_total / allowed_risk) if allowed_risk > 0 else 0.0

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
            tracker.set(
                ot.side,
                ot.qty,
                ot.stop,
                ot.target,
                ot.parent_id,
                ot.stop_id,
                ot.target_id,
                ot.entry_ts_utc,
            )
            open_positions.set(1)
        else:
            store.clear_open_trade()

    # exec callback
    def on_exec(trade, fill):
        if not tracker.open:
            return
        reason = tracker.classify_fill(getattr(trade.order, "orderId", -1))
        if reason in ("STOP", "TARGET"):
            orders_filled.labels(side=tracker.side, reason=reason).inc()
            risk.record_trade_outcome_R(1.0 if reason == "TARGET" else -1.0)
            log_fill(
                log_path,
                symbol=cfg["symbol"],
                side=tracker.side,
                qty=tracker.qty,
                entry_price=None,
                exit_price=None,
                exit_reason=reason,
                pnl_dollars=None,
                R=(1.0 if reason == "TARGET" else -1.0),
                order_id=getattr(trade.order, "orderId", None),
            )
            # daily stats
            if reason == "TARGET":
                day_stats["targets"] = day_stats.get("targets", 0) + 1
                day_stats["realized_R"] = day_stats.get("realized_R", 0.0) + 1.0
            elif reason == "STOP":
                day_stats["stops"] = day_stats.get("stops", 0) + 1
                day_stats["realized_R"] = day_stats.get("realized_R", 0.0) - 1.0
            store.clear_open_trade()
            store.save_risk(
                RiskState(
                    realized_R=risk.realized_R,
                    consec_losses=risk.consec_losses,
                    halted=risk.halted,
                )
            )
            if tracker.entry_ts:
                dt_ms = (
                    datetime.now(timezone.utc) - tracker.entry_ts
                ).total_seconds() * 1000
                latency_ms.observe(dt_ms)
            tracker.reset()
            open_positions.set(0)

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
            store.save_risk(
                RiskState(
                    realized_R=risk.realized_R,
                    consec_losses=risk.consec_losses,
                    halted=True,
                )
            )

            await ibc.disconnect()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # --- daily stats & EOD summary ---
    day_stats = {
        "submits": 0,
        "targets": 0,
        "stops": 0,
        "realized_R": 0.0,
        "watchdog_events": 0,
        "first_signal": None,
        "last_signal": None,
    }

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

    def _effective_md_type(ibc_obj, cfg_obj):
        raw = getattr(getattr(ibc_obj.ib, "client", None), "marketDataType", None)
        try:
            code = int(raw)
        except Exception:
            code = 0
        if code in (1, 2, 3, 4):
            return code
        return getattr(
            ibc_obj, "_md_type", 3 if cfg_obj["ibkr"].get("use_delayed", False) else 1
        )

    md_actual = _effective_md_type(ibc, cfg)
    max_lag = int(cfg.get("ibkr", {}).get("max_data_lag_sec", 120))
    if md_actual in (3, 4):  # delayed feed in practice
        max_lag = max(max_lag, 1200)  # bump to 20 min so you don't choke
    else:
        max_lag = max(60, max_lag)  # keep at least 60–120s for 1m bars
    print(
        f"[INFO] data staleness gate = {max_lag}s (requested_delayed={cfg['ibkr'].get('use_delayed', False)} actual_md={md_actual})"
    )
    if dry_run:
        print("[DRY-RUN] enabled: no orders will be sent")
    if dry_run and dry_run_ignore_gates:
        print("[DRY-RUN] ignoring staleness + session windows")
    heartbeat_next = 0.0

    # --- de-dupe & cooldown state ---
    last_signal_dt_iso = None
    last_sig_idx = -10_000
    cooldown_bars = int(
        cfg.get("execution", {}).get("cooldown_bars", 12)
    )  # default 12 bars

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
                print(
                    f"[HEARTBEAT] last_bar={last_dt.isoformat()} age={age_sec:.1f}s max_lag={max_lag}"
                )
                heartbeat_next = now_ts + 30  # every 30s

            if not (dry_run and dry_run_ignore_gates) and age_sec > max_lag:
                continue  # too stale -> skip signals/orders
        except Exception:
            pass

        bar = fdf.iloc[-1]

        # --- EOD summary trigger (once per day) ---
        now_loc = datetime.now(tz_local)
        today_str = now_loc.date().isoformat()
        if today_str != summary_emitted_for and (now_loc.hour, now_loc.minute) >= (
            flat_h,
            flat_m,
        ):
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
            day_stats = {
                "submits": 0,
                "targets": 0,
                "stops": 0,
                "realized_R": 0.0,
                "watchdog_events": 0,
                "first_signal": None,
                "last_signal": None,
            }
            risk.reset_day()
            prev_date = bar["date"]
            store.save_risk(
                RiskState(
                    realized_R=risk.realized_R,
                    consec_losses=risk.consec_losses,
                    halted=risk.halted,
                )
            )

        if not (dry_run and dry_run_ignore_gates) and not risk.can_trade_now(
            bar["t_local"]
        ):
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
            limit_px = tick_round(
                last_close + dx if side.upper() == "BUY" else last_close - dx
            )
            entry_ref = float(getattr(sig["order"], "entry", bar["close"]))

            stop_dist = abs(float(stop) - entry_ref)
            target_dist = abs(float(target) - entry_ref)

            if side.upper() == "BUY":
                stop = tick_round(limit_px - stop_dist)
                target = tick_round(limit_px + target_dist)
            else:  # SELL
                stop = tick_round(limit_px + stop_dist)
                target = tick_round(limit_px - target_dist)

            if dry_run:
                # DRY RUN: only log what would be sent, don't touch IB or state
                ts = str(bar.get("t_local", rb.df.iloc[-1]["datetime"]))

                # risk math
                usd_per_point = float(cfg["fees"]["tick_value"]) / float(
                    cfg["fees"]["tick_size"]
                )
                stop_dist_pts = abs(limit_px - stop)
                risk_per_contract = stop_dist_pts * usd_per_point
                allowed_risk = (
                    float(cfg["risk"]["account_equity"])
                    * float(cfg["risk"]["risk_pct"])
                    * float(cfg["risk"].get("risk_buffer_pct", 1.0))
                )
                risk_total = risk_per_contract * int(qty)
                ratio = (risk_total / allowed_risk) if allowed_risk > 0 else 0.0
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
                        trades = await ibc.place_bracket_limit(
                            side,
                            qty,
                            limit_px,
                            stop,
                            target,
                            tif=tif,
                            outsideRth=outside_rth,
                        )
                    else:
                        trades = await ibc.place_bracket_market(
                            side, qty, stop, target, tif=tif, outsideRth=outside_rth
                        )
                except Exception as e:
                    print(
                        f"[WARN] place_bracket_limit failed ({e}); falling back to market"
                    )
                    trades = await ibc.place_bracket_market(
                        side, qty, stop, target, tif=tif, outsideRth=outside_rth
                    )

                parent_id = trades[0].order.orderId
                tp_id = trades[1].order.orderId
                sl_id = trades[2].order.orderId
                orders_submitted.labels(side=side).inc()
                open_positions.set(1)
                tracker.set(side, qty, stop, target, parent_id, sl_id, tp_id, t0)

                # daily stats (real submits only)
                if day_stats["first_signal"] is None:
                    day_stats["first_signal"] = str(
                        bar.get("t_local", rb.df.iloc[-1]["datetime"])
                    )
                day_stats["last_signal"] = str(
                    bar.get("t_local", rb.df.iloc[-1]["datetime"])
                )
                day_stats["submits"] = day_stats.get("submits", 0) + 1

                store.save_open_trade(
                    OpenTradeState(
                        symbol=cfg["symbol"],
                        side=side,
                        qty=qty,
                        stop=stop,
                        target=target,
                        parent_id=parent_id,
                        stop_id=sl_id,
                        target_id=tp_id,
                        entry_ts_utc=t0.isoformat(),
                    )
                )
                log_submit(
                    log_path,
                    symbol=cfg["symbol"],
                    side=side,
                    qty=qty,
                    stop=stop,
                    target=target,
                    parent_id=parent_id,
                    tp_id=tp_id,
                    sl_id=sl_id,
                )
                store.save_risk(
                    RiskState(
                        realized_R=risk.realized_R,
                        consec_losses=risk.consec_losses,
                        halted=risk.halted,
                    )
                )

            # remember we acted on this bar (even in dry-run)
        last_signal_dt_iso = bar_ts_iso
        last_sig_idx = len(fdf) - 1
    await shutdown()
    return


# ------------------------------------------------------------------
# Portfolio-mode launcher
# ------------------------------------------------------------------
async def run_portfolio(args):
    portfolio_file = f"config/portfolios/{args.portfolio}.yaml"
    if not os.path.exists(portfolio_file):
        print(f"[PORTFOLIO] file not found: {portfolio_file}")
        sys.exit(2)

    with open(portfolio_file) as f:
        pf = yaml.safe_load(f)

    stop_all = asyncio.Event()

    tasks: List[asyncio.Task] = []

    # Start microlots
    for i, lot in enumerate(pf.get("microlots", [])):
        t = asyncio.create_task(
            run_microlot(lot, args.dry_run, client_id=2 + i, stop_all=stop_all),
            name=f"microlot:{lot.get('name','?')}"
        )
        tasks.append(t)

    print(f"[PORTFOLIO] started {len(tasks)} microlots")

    # Ctrl-C / SIGTERM -> flip stop flag + cancel tasks
    loop = asyncio.get_running_loop()

    def _request_shutdown():
        if not stop_all.is_set():
            print("\n[PORTFOLIO] shutdown requested...")
            stop_all.set()
        for t in tasks:
            t.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # macOS usually supports this, but keep fallback
            pass

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("[PORTFOLIO] stopped")



async def run_microlot(lot: Dict[str, Any], dry_run: bool, client_id: int, stop_all: asyncio.Event):
    """
    Run a *single* microlot (EQ-SWI, CRYPTO, etc.) in its own event-loop task.
    Each gets its own broker instance, risk governor, rolling bars, etc.
    """
    stop = stop_all
    name = lot["name"]
    is_live = False

    print(
        f"[{name}] starting microlot (universe={lot['universe']}, broker={lot['broker']})"
    )
    rows_written = 0

    # ------------------------------------------------------------------
    # 1. pick broker class
    # ------------------------------------------------------------------
    broker_map = {
        "ibkr": IbkrBroker,
        "ibkr_fractional": IbkrFractional,
        "binanceus": CcxtBinanceus,
    }
    broker_cls = broker_map.get(lot["broker"])
    if broker_cls is None:
        print(
            f"[{name}] broker {lot['broker']} unavailable (missing deps); skipping microlot"
        )
        return
    
    if lot["broker"] == "ibkr":
        raise RuntimeError(
            f"[{name}] Futures broker 'ibkr' is disabled for this portfolio. Use 'ibkr_fractional'."
        )

    # ------------------------------------------------------------------
    # 2. load universe (symbols + tick_size + point_value + strategy + rotate)
    # ------------------------------------------------------------------
    uni_path = f"config/universes/{lot['universe']}.yaml"
    with open(uni_path, "r") as f:
        uni = yaml.safe_load(f)

    symbols: list[str] = uni["symbols"]
    if not symbols:
        raise ValueError(f"[{name}] Universe {lot['universe']} has no symbols")
    rotate = bool(uni.get("rotate", False))
    # session windows from universe yaml
    # expected in yaml: session_windows: - ["09:30", "16:00"]
    raw_windows = uni.get("session_windows") or []
    windows = [{"start": a, "end": b} for (a, b) in raw_windows]

    # safety fallback so you don't silently block trading if yaml is missing
    if not windows:
        windows = [{"start": "00:00", "end": "23:59"}]

    tick_size = float(uni.get("tick_size", 0.01))
    point_value = float(uni.get("point_value", 1.0))

    # strategy params per universe, with sane defaults
    strat_defaults = {
        "orb_minutes": 15,
        "atr_len": 14,
        "break_eps_ticks": 2,
        "atr_mult": 1.0,
        "target_R": 1.0,
        "slope_min": 0.0,
        "stop_pad_ticks": 2,
        "trail_pad_ticks": 2,
    }
    strat_cfg_raw = {**strat_defaults, **uni.get("strategy", {})}

    orb_min = int(strat_cfg_raw["orb_minutes"])

    # ------------------------------------------------------------------
    # 3. broker connect
    # ------------------------------------------------------------------
    if lot["broker"] == "binanceus":
        api_key = os.getenv("BINANCEUS_API_KEY")
        secret = os.getenv("BINANCEUS_SECRET")
        if not api_key or not secret:
            print(
                f"[{name}] missing BINANCEUS_API_KEY / BINANCEUS_SECRET – skipping microlot"
            )
            return
        broker = broker_cls(api_key, secret, sandbox=False)
        use_delayed = False  # N/A for crypto
    else:  # IBKR path (paper/live based on your TWS)
        broker = broker_cls("127.0.0.1", 7497, client_id=client_id)
        use_delayed = True  # matches your existin delayed config

    await broker.connect()

    # market data mode (no-op for ccxt broker)
    try:
        broker.set_market_data_type(3 if use_delayed else 1)
        broker.enforce_market_data_guard(use_delayed=use_delayed, allow_mismatch=False)
        print(f"[{name}] md_type={getattr(broker, '_md_type', '?')} ib_actual={getattr(getattr(broker.ib,'client',None),'marketDataType','?')}")
    except Exception as e:
        print(f"[{name}] market-data guard warning: {e}")

    # ------------------------------------------------------------------
    # 4. pick and resolve a tradable symbol
    #    try multiple symbols from the universe instead of just one
    # ------------------------------------------------------------------
    candidates = symbols.copy()
    if rotate and len(candidates) > 1:
        import random

        random.shuffle(candidates)

    symbol = None
    for sym in candidates:
        try:
            await broker.resolve_contract(sym)
            symbol = sym
            print(
                f"[{name}] trading {symbol} (tick_size={tick_size}, point_value={point_value})"
            )
            break
        except Exception as e:
            print(f"[{name}] failed to qualify {sym}: {e} – trying next symbol")

    if symbol is None:
        print(
            f"[{name}] no symbols in universe {lot['universe']} could be qualified – skipping microlot"
        )
        await broker.disconnect()
        return

    # ------------------------------------------------------------------
    # 4b. microlot state file (JSON)
    # ------------------------------------------------------------------
    state_path = Path("state") / f"{name}.json"
    state = MicrolotState.load(
        state_path,
        name=name,
        universe=lot["universe"],
        broker=lot["broker"],
        cash_alloc=float(lot["cash_alloc"]),
        max_risk_per_trade=float(lot["max_risk_per_trade"]),
    )
    state.symbol = symbol
    # initial heartbeat when we start streaming
    state.last_heartbeat_ts = datetime.now(timezone.utc).isoformat()
    state.save(state_path)

    # ------------------------------------------------------------------
    # 5. Rolling bars + strategy + risk
    # ------------------------------------------------------------------
    rb = RollingBars("America/Chicago", orb_min, symbol)

    strat_cfg = StratConfig(
        orb_minutes=int(strat_cfg_raw["orb_minutes"]),
        atr_len=int(strat_cfg_raw["atr_len"]),
        break_eps_ticks=int(strat_cfg_raw["break_eps_ticks"]),
        atr_mult=float(strat_cfg_raw["atr_mult"]),
        target_R=float(strat_cfg_raw["target_R"]),
        slope_min=float(strat_cfg_raw["slope_min"]),
        stop_pad_ticks=int(strat_cfg_raw["stop_pad_ticks"]),
        trail_pad_ticks=int(strat_cfg_raw["trail_pad_ticks"]),
        tick_size=float(tick_size),
        tick_value=float(point_value),
    )
    strat = HybridOrbVwap(strat_cfg)

    risk_cfg = RiskConfig(
        account_equity=float(lot["cash_alloc"]),
        risk_pct=float(lot["max_risk_per_trade"]),
        max_daily_loss_R=2.0,
        max_consec_losses=3,
        tick_value=float(point_value),
        flat_time="16:00",
        news_lockout_minutes=0,
    )
    risk = RiskGovernor(risk_cfg)

    # ------------------------------------------------------------------
    # 5b. per-symbol bar log (build our own history)
    # ------------------------------------------------------------------
    print(f"[{name}] CWD={os.getcwd()}")

    bar_log_path = Path("data") / f"{symbol}_live_1m.csv"
    bar_log_path.parent.mkdir(parents=True, exist_ok=True)

    def append_bar_to_csv(bar_norm: dict) -> None:
        header = ["datetime", "open", "high", "low", "close", "volume"]
        new_file = not bar_log_path.exists()
        with bar_log_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(header)
            w.writerow([
                bar_norm["datetime"],
                bar_norm["open"],
                bar_norm["high"],
                bar_norm["low"],
                bar_norm["close"],
                bar_norm["volume"],
            ])

    # ------------------------------------------------------------------
    # 6. on_bar: log -> features -> strategy
    # ------------------------------------------------------------------
    MIN_FEATURE_ROWS = 5
    throttle = {"last_print_ts": 0.0,
                "last_signal_dt": None} # throttle console spam

    async def on_bar(bar: dict):
        if stop.is_set():
            return
        # normalize incoming bar shape
        dt = bar.get("datetime") or bar.get("ts")

        bar_norm = {
            "datetime": dt,
            "open": float(bar.get("open", 0.0)),
            "high": float(bar.get("high", 0.0)),
            "low": float(bar.get("low", 0.0)),
            "close": float(bar.get("close", 0.0)),
            "volume": float(bar.get("volume", 0.0)),
        }

        # persist heartbeat + symbol state
        now_iso = datetime.now(timezone.utc).isoformat()
        state.last_heartbeat_ts = now_iso
        state.save(state_path)

        bar_dt = bar.get("datetime") or bar.get("ts")

        # ALWAYS log bars to CSV
        append_bar_to_csv(bar_norm)

        # build features
        rb.add(bar_norm)
        fdf = rb.features()
        f_len = 0 if fdf is None else len(fdf)

        # print once every ~20s so you know it's alive
        now_ts = datetime.now(timezone.utc).timestamp()
        if now_ts - throttle["last_print_ts"] >= 20:
            print(f"[{name}] bars_ok dt={dt} features_len={f_len} csv={bar_log_path}")
            throttle["last_print_ts"] = now_ts


        if fdf is None or f_len < MIN_FEATURE_ROWS:
            return

        latest = fdf.iloc[-1]
        last_seen_dt = getattr(state, "last_seen_dt", None)
        if last_seen_dt == bar_norm["datetime"]:
            return
        state.last_seen_dt = bar_norm["datetime"]
        state.save(state_path)

        import datetime as dt
        import pytz
        # Make sure latest has t_local, since the strategy uses it.
        # If your RollingBars.features() already includes t_local, this won't overwrite it.
        if "t_local" not in latest.index:
            iso = bar_norm["datetime"]
            ts_utc = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            ts_ct = ts_utc.astimezone(pytz.timezone("America/Chicago"))
            latest.loc["t_local"] = ts_ct.strftime("%H:%M")

        decision = strat.maybe_signal(latest, windows=windows, risk=risk)

        if decision is None:
            return
        
        sig_dt = bar_norm["datetime"]
        if throttle["last_signal_dt"] == sig_dt:
            return
        throttle["last_signal_dt"] = sig_dt
        


        order = decision["order"]
        bracket = decision["bracket"]

        state.trade_count += 1
        state.last_decision_ts = now_iso
        state.last_side = order.side
        state.last_qty = float(order.qty)
        state.last_stop = float(bracket.stop_price)
        state.last_target = float(bracket.target_price)
        state.symbol = symbol
        state.save(state_path)

        if dry_run:
            print(f"[{name}] DRY-RUN signal {order.side} {order.qty} {order.symbol} "
                f"stop={bracket.stop_price:.4f} target={bracket.target_price:.4f}")
            return

        tif = "DAY" if lot["broker"].startswith("ibkr") else None
        outside_rth = False if lot["broker"].startswith("ibkr") else None

        try:
            trades = await broker.place_bracket_market(
                side=order.side,
                qty=int(order.qty),
                stop_price=float(bracket.stop_price),
                target_price=float(bracket.target_price),
                tif=tif,
                outsideRth=outside_rth,
            )
            print(f"[{name}] submitted bracket for {order.symbol}, trades={trades}")
        except Exception as e:
            print(f"[{name}] ERROR placing bracket: {e}")


    # ------------------------------------------------------------------
    # 7. start stream + wait
    # ------------------------------------------------------------------
    def _task_done(t: asyncio.Task):
        exc = t.exception()
        if exc:
            print(f"[{name}] bar_task crashed: {type(exc).__name__}: {exc}")
        else:
            print(f"[{name}] bar_task ended cleanly")
    
    
    
    bar_task = asyncio.create_task(
        broker.stream_realtime_bars(
            on_bar=on_bar,
            what_to_show="TRADES",
            bar_size_secs=60,
            preload_days=2,
            prefill_n=2000,
        )
    )

    async def _watch_task(t: asyncio.Task):
        try:
            await t
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[{name}] bar_task crashed: {type(e).__name__}: {e}")
            stop.set()

    watch_task = asyncio.create_task(_watch_task(bar_task))


    bar_task.add_done_callback(_task_done)

    try:
        # run until cancelled from outside (Ctrl-C or SIGINT in run_portfolio)
        while not stop.is_set():
            await asyncio.sleep(0.2)

    except asyncio.CancelledError:
        pass
    finally:
        bar_task.cancel()
        watch_task.cancel()
        with suppress(Exception):
            await bar_task
        with suppress(Exception):
            await watch_task
        await broker.disconnect()
        print(f"[{name}] microlot stopped and broker disconnected")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    # legacy (optional): direct file; ignored if --env is used
    ap.add_argument("--config", help="Path to environment YAML (legacy)")

    # new structure
    ap.add_argument(
        "--env", choices=["paper", "live"], help="Loads config/env/<env>.yaml"
    )
    ap.add_argument(
        "--profile",
        choices=["futures", "equities"],
        help="Loads config/profile/<profile>.yaml",
    )
    ap.add_argument(
        "--config-extra",
        dest="config_extra",
        help="Optional extra YAML to merge last (overrides all)",
    )
    ap.add_argument(
        "--strict-profile",
        action="store_true",
        help="Abort if profile doesn’t match symbol",
    )

    # your existing run flags
    ap.add_argument("--symbol", default="MES", help="Instrument")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Log orders that would be placed; do not touch IB/account state",
    )
    ap.add_argument(
        "--dry-run-replay",
        type=int,
        default=0,
        help="In dry-run, replay last N bars and print 'would place' lines, then exit",
    )
    ap.add_argument(
        "--dry-run-ignore-gates",
        action="store_true",
        help="In dry-run, ignore staleness and session windows",
    )
    ap.add_argument(
        "--portfolio",
        default=None,
        help="Run a multi-asset micro-lot portfolio (config/portfolios/<name>.yaml)",
    )

    args = ap.parse_args()
    try:
        asyncio.run(main(args=args))
    except KeyboardInterrupt:
        print("\n[MAIN] KeyboardInterrupt received - shutting down portfolio...")
