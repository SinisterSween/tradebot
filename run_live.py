import asyncio, yaml, pandas as pd, signal, sys, argparse, os, json, csv
from dotenv import load_dotenv
load_dotenv()
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from contextlib import suppress
from typing import List, Dict, Any
from dataclasses import dataclass, asdict
from pathlib import Path
from run_backtest import _symbol_to_csv_stem
from trader.telemetry.metrics import data_age_seconds

# --- make ib_insync play nicely with asyncio ---
try:
    from ib_insync import util as ib_util

    ib_util.patchAsyncio()
except Exception:
    pass

from trader.brokers.ibkr import IbkrBroker
from trader.brokers.ibkr_fractional import IbkrFractional

try:
    from trader.brokers.ccxt_binanceus import CcxtBinanceus, BracketOrphanError
except ModuleNotFoundError as e:
    print(f"[WARN] ccxt/crypto broker unavailable: {e}")
    CcxtBinanceus = None
    BracketOrphanError = Exception  # fallback so except clauses don't break
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.engine.backtest import prepare_bars
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig as OrbStratConfig
from trader.strategies.rsi_mean_reversion import RsiMeanReversion, StratConfig as RsiStratConfig
from trader.strategies.trend_pullback import TrendPullback, StratConfig as TrendStratConfig
from trader.strategies.vwap_reversion import VwapReversion, StratConfig as VwapStratConfig
from trader.engine.portfolio_selector import PortfolioSelector, Candidate
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

# Keep the old name for any code paths that reference StratConfig directly
StratConfig = OrbStratConfig

# Map every name variant your universe YAMLs might use → canonical key
_STRATEGY_NAME_MAP = {
    "hybrid_orb_vwap":    "hybrid_orb_vwap",
    "hybridorbvwap":      "hybrid_orb_vwap",
    "orb":                "hybrid_orb_vwap",
    "tinycapbreakout":    "hybrid_orb_vwap",   # tiny_cap.yaml uses ORB-style params
    "leveredetfbreakout": "hybrid_orb_vwap",   # levered_etf.yaml uses ORB-style params
    "rsi_mean_reversion": "rsi_mean_reversion",
    "rsimeanreversion":   "rsi_mean_reversion",
    "rsi":                "rsi_mean_reversion",
    "trend_pullback":     "trend_pullback",
    "trendpullback":      "trend_pullback",
    "vwap_reversion":     "vwap_reversion",
    "vwapreversion":      "vwap_reversion",
    "vwap":               "vwap_reversion",
}


def _resolve_strategy_type(uni: dict, raw: dict) -> str:
    """
    Find the strategy type from the universe YAML, checking in priority order:
      1. uni["strategy_type"]  – new canonical field
      2. uni["strategy_name"]  – top-level descriptive name (sp500_fractional, tiny_cap, levered_etf)
      3. raw["name"]           – nested strategy.name (crypto_micro_mr, crypto_micro)
    Returns a canonical type string, defaulting to "hybrid_orb_vwap".
    """
    raw_name = (
        uni.get("strategy_type")
        or uni.get("strategy_name")
        or raw.get("name")
        or "hybrid_orb_vwap"
    )
    key = str(raw_name).lower().replace(" ", "_").replace("-", "_")
    return _STRATEGY_NAME_MAP.get(key, "hybrid_orb_vwap")


def build_strategy(strategy_type: str, raw: dict, tick_size: float, tick_value: float):
    """
    Build a strategy instance from a raw config dict.
    Pass the result of _resolve_strategy_type() as strategy_type.
    All four strategies expose: maybe_signal(bar, windows, risk) + score_signal(bar, decision).
    """
    t = str(strategy_type or "hybrid_orb_vwap").lower().strip()

    if t == "rsi_mean_reversion":
        return RsiMeanReversion(RsiStratConfig(
            rsi_len=int(raw.get("rsi_len", 14)),
            rsi_buy_below=float(raw.get("rsi_buy_below", 28.0)),
            rsi_sell_above=float(raw.get("rsi_sell_above", 72.0)),
            atr_mult=float(raw.get("atr_mult", 1.0)),
            target_R=float(raw.get("target_R", 0.8)),
            stop_pad_ticks=int(raw.get("stop_pad_ticks", 2)),
            allow_longs=bool(raw.get("allow_longs", True)),
            allow_shorts=bool(raw.get("allow_shorts", False)),
            slope_max=float(raw.get("slope_max", 0.0)),
            min_atr_pct=float(raw.get("min_atr_pct", 0.0)),
            tick_size=float(tick_size),
            tick_value=float(tick_value),
            target_vwap=bool(raw.get("target_vwap", True)),
            vwap_dist_atr_min=float(raw.get("vwap_dist_atr_min", 0.0)),
            require_cross=bool(raw.get("require_cross", True)),
            min_minutes_between_trades=int(raw.get("min_minutes_between_trades", 30)),
            max_trades_per_day=int(raw.get("max_trades_per_day", 6)),
            ema_slope_max=float(raw.get("ema_slope_max", 0.0)),
            ema_slope_atr_min=float(raw.get("ema_slope_atr_min", -0.2)),
            ema_dist_atr_min=float(raw.get("ema_dist_atr_min", 0.0)),
            ema_dist_atr_max=float(raw.get("ema_dist_atr_max", 0.0)),
            exit_on_ema=bool(raw.get("exit_on_ema", False)),
            exit_ema_len=int(raw.get("exit_ema_len", 20)),
            exit_ema_side=str(raw.get("exit_ema_side", "cross")),
            min_vwap_target_R=float(raw.get("min_vwap_target_R", 0.0)),
            vwap_entry_min_atr=float(raw.get("vwap_entry_min_atr", 0.0)),
            vwap_entry_max_atr=float(raw.get("vwap_entry_max_atr", 0.0)),
            use_session_open_stop=bool(raw.get("use_session_open_stop", False)),
            session_open_stop_atr_pad=float(raw.get("session_open_stop_atr_pad", 0.0)),
            use_early_range_target=bool(raw.get("use_early_range_target", False)),
            early_range_mult=float(raw.get("early_range_mult", 1.0)),
            early_min_range_atr=float(raw.get("early_min_range_atr", 0.0)),
            require_early_done=bool(raw.get("require_early_done", True)),
        ))

    if t == "trend_pullback":
        return TrendPullback(TrendStratConfig(
            atr_len=int(raw.get("atr_len", 14)),
            atr_mult=float(raw.get("atr_mult", 1.0)),
            target_R=float(raw.get("target_R", 1.5)),
            slope_min=float(raw.get("slope_min", 0.0)),
            pullback_atr=float(raw.get("pullback_atr", 0.5)),
            stop_pad_ticks=int(raw.get("stop_pad_ticks", 2)),
            tick_size=float(tick_size),
            tick_value=float(tick_value),
            min_atr_pct=float(raw.get("min_atr_pct", 0.0)),
            max_bar_range_atr=float(raw.get("max_bar_range_atr", 0.0)),
            min_notional_usd=float(raw.get("min_notional_usd", 0.0)),
        ))

    if t == "vwap_reversion":
        return VwapReversion(VwapStratConfig(
            atr_len=int(raw.get("atr_len", 14)),
            atr_mult=float(raw.get("atr_mult", 1.0)),
            target_R=float(raw.get("target_R", 1.0)),
            slope_max=float(raw.get("slope_max", 0.05)),
            stop_pad_ticks=int(raw.get("stop_pad_ticks", 2)),
            tick_size=float(tick_size),
            tick_value=float(tick_value),
            min_atr_pct=float(raw.get("min_atr_pct", 0.0)),
            target_vwap=bool(raw.get("target_vwap", True)),
            max_bar_range_atr=float(raw.get("max_bar_range_atr", 0.0)),
            min_vwap_target_R=float(raw.get("min_vwap_target_R", 0.0)),
            ema_slope_max=float(raw.get("ema_slope_max", 0.0)),
            ema_max_dist_atr=float(raw.get("ema_max_dist_atr", 0.0)),
            vwap_entry_min_atr=float(raw.get("vwap_entry_min_atr", 0.0)),
            vwap_entry_max_atr=float(raw.get("vwap_entry_max_atr", 0.0)),
            orb_minutes=int(raw.get("orb_minutes", 0)),
            allow_longs=bool(raw.get("allow_longs", True)),
            allow_shorts=bool(raw.get("allow_shorts", True)),
            ema_slope_buy_min=float(raw.get("ema_slope_buy_min", -999.0)),
            ema_slope_sell_max=float(raw.get("ema_slope_sell_max", 999.0)),
        ))

    # default: hybrid_orb_vwap
    return HybridOrbVwap(OrbStratConfig(
        orb_minutes=int(raw.get("orb_minutes", 15)),
        atr_len=int(raw.get("atr_len", 14)),
        break_eps_ticks=int(raw.get("break_eps_ticks", 2)),
        atr_mult=float(raw.get("atr_mult", 1.0)),
        target_R=float(raw.get("target_R", 1.0)),
        slope_min=float(raw.get("slope_min", 0.0)),
        stop_pad_ticks=int(raw.get("stop_pad_ticks", 2)),
        trail_pad_ticks=int(raw.get("trail_pad_ticks", 2)),
        tick_size=float(tick_size),
        tick_value=float(tick_value),
    ))

def load_features_from_csv(csv_path: str, *, tz: str, orb_minutes: int, symbol: str, tail_n: int = 2500):
    """
    Reads last N rows from a symbol CSV, builds feature DF via prepare_bars, returns it.
    Assumes CSV has: datetime, open, high, low, close, volume
    datetime is ISO string (UTC).
    """
    p = Path(csv_path)
    if not p.exists():
        return None

    try:
        # tail read (fast enough for N~2500). If you want super-fast later, we can do file tail parsing.
        df = pd.read_csv(p)
        if df.empty:
            return None
        if len(df) > tail_n:
            df = df.iloc[-tail_n:]

        # normalize
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
        df = df.dropna(subset=["datetime"])
        if df.empty:
            return None

        # prepare_bars expects columns: datetime, ohlcv, symbol + tz/orb
        return prepare_bars(df, tz, orb_minutes, symbol=symbol, verbose=False)
    except Exception:
        return None

def pick_best_signal_for_universe(
    *,
    symbols: list[str],
    csv_dir: str,
    strat,
    windows,
    risk,
    tz: str,
    orb_minutes: int,
    min_score: float = 0.0,
):
    """
    For each symbol:
      - build features from CSV
      - run maybe_signal on latest bar
      - score_signal and keep best
    Returns (best_symbol, best_decision, best_score, best_bar) or (None, None, 0.0, None)
    """
    best = (None, None, 0.0, None)

    for sym in symbols:
        csv_path = str(Path(csv_dir) / f"{sym.strip().replace('/','_').upper()}_live_1m.csv")
        fdf = load_features_from_csv(csv_path, tz=tz, orb_minutes=orb_minutes, symbol=sym)
        if fdf is None or fdf.empty:
            continue

        bar = fdf.iloc[-1]
        try:
            decision = strat.maybe_signal(bar, windows, risk)
            if not decision:
                continue

            score = float(getattr(strat, "score_signal")(bar, decision))
            if score < float(min_score):
                continue

            if score > best[2]:
                best = (sym, decision, score, bar)
        except Exception:
            continue

    return best

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
        return prepare_bars(tmp, self.tz, self.orb_minutes, symbol=self.symbol, verbose=False)


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

    # Shared status dict for heartbeat + daily summary
    lot_status: Dict[str, Dict] = {
        lot["name"]: {
            "trades_today":  0,
            "targets_today": 0,
            "stops_today":   0,
            "pnl_today":     0.0,
            "open":          False,
            "open_symbol":   "",
            "open_side":     "",
        }
        for lot in pf.get("microlots", [])
    }

    tasks: List[asyncio.Task] = []

    # Start microlots
    for i, lot in enumerate(pf.get("microlots", [])):
        t = asyncio.create_task(
            run_microlot(lot, args.dry_run, client_id=2 + i, stop_all=stop_all,
                         lot_status=lot_status),
            name=f"microlot:{lot.get('name','?')}"
        )
        tasks.append(t)

    # Start heartbeat + daily summary background task
    try:
        from scripts.heartbeat import heartbeat_loop as _heartbeat_loop
        ht_task = asyncio.create_task(
            _heartbeat_loop(lot_status, stop_all),
            name="heartbeat"
        )
        tasks.append(ht_task)
        print("[PORTFOLIO] heartbeat task started (ping every 4 h, daily summary at midnight UTC)")
    except Exception as _he:
        print(f"[PORTFOLIO] heartbeat unavailable: {_he}")

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
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("[PORTFOLIO] stopped")



async def run_microlot(lot: Dict[str, Any], dry_run: bool, client_id: int, stop_all: asyncio.Event, lot_status: Dict = None):
    """
    Run a *single* microlot (EQ-SWI, CRYPTO, etc.) in its own event-loop task.
    Each gets its own broker instance, risk governor, rolling bars, etc.
    """
    stop = stop_all
    _lot_stop = asyncio.Event()   # lot-local stop; set on symbol rotation (≠ global stop_all)
    name = lot["name"]
    is_live = False

    # per-lot dry_run: true overrides global flag (but global --dry-run also forces all lots dry)
    dry_run = dry_run or bool(lot.get("dry_run", False))

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

    # Normalize symbols: can be list[str] OR list[dict] (crypto_all per-symbol format).
    # Dicts with "locked: false" are skipped (not yet tuned).
    _symbols_raw = uni["symbols"]
    _sym_overrides: dict = {}
    _sym_list: list = []
    for _s in _symbols_raw:
        if isinstance(_s, dict):
            _sym_str = _s.get("symbol", "")
            if not _sym_str:
                continue
            if "locked" in _s and not _s["locked"]:
                continue
            _sym_overrides[_sym_str] = _s
            _sym_list.append(_sym_str)
        else:
            _sym_list.append(str(_s))
    symbols = _sym_list

    if not symbols:
        raise ValueError(f"[{name}] Universe {lot['universe']} has no symbols")
    rotate = bool(uni.get("rotate", False))
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

    # session_windows: prefer strategy-level (where all our YAMLs put it),
    # fall back to root-level, then default to 24/7
    _raw_win = strat_cfg_raw.get("session_windows") or uni.get("session_windows") or []
    windows = _norm_windows(_raw_win) or [{"start": "00:00", "end": "23:59"}]

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

    try:
        await broker.connect()
    except Exception as e:
        print(f"[{name}] broker connect failed: {e} – skipping microlot")
        return

    # market data mode (no-op for ccxt broker)
    try:
        broker.set_market_data_type(3 if use_delayed else 1)
        broker.enforce_market_data_guard(use_delayed=use_delayed, allow_mismatch=False)
        print(f"[{name}] md_type={getattr(broker, '_md_type', '?')} ib_actual={getattr(getattr(broker.ib,'client',None),'marketDataType','?')}")
    except Exception as e:
        print(f"[{name}] market-data guard warning: {e}")

    # ------------------------------------------------------------------
    # 4. pick and resolve a tradable symbol
    #    pre-score all candidates from CSV, then try in score order
    # ------------------------------------------------------------------
    candidates = symbols.copy()
    if rotate and len(candidates) > 1:
        import random
        random.shuffle(candidates)

    # Dynamic scanner: when scan_universe: true in the universe YAML, replace the
    # static symbol list with a live-scanned ranking.
    # - Crypto (Binance.US): scans all ~100 USDT pairs by ATR×trend
    # - Equity (IBKR):       scores the universe symbol list by ATR×trend+gap
    # Runs at lot startup; falls back to static symbols on any error.
    _scan_universe = bool(uni.get("scan_universe", False))
    _scan_cfg = uni.get("scan_params", {})
    if _scan_universe and CcxtBinanceus is not None and isinstance(broker, CcxtBinanceus):
        # --- Crypto path ---
        try:
            from trader.engine.crypto_scanner import scan_top_symbols
            _scanned = await scan_top_symbols(
                broker.exchange,
                top_n=int(_scan_cfg.get("top_n", 5)),
                min_volume_usd=float(_scan_cfg.get("min_volume_usd", 100_000)),
                min_atr_pct=float(_scan_cfg.get("min_atr_pct", 0.003)),
                prefer_trending=bool(_scan_cfg.get("prefer_trending", True)),
                verbose=True,
            )
            if _scanned:
                print(f"[{name}] scanner selected {len(_scanned)} candidates: {_scanned}")
                candidates = _scanned
                try:
                    from scripts.heartbeat import notify_lot_started
                    notify_lot_started(name, _scanned[0], _scanned)
                except Exception:
                    pass
            else:
                print(f"[{name}] scanner returned no results — using static symbols")
        except Exception as _scan_e:
            print(f"[{name}] scanner error: {_scan_e} — using static symbols")

    elif _scan_universe and isinstance(broker, (IbkrBroker, IbkrFractional)):
        # --- Equity path: score the universe symbol list via IBKR historical bars ---
        try:
            from trader.engine.equity_scanner import scan_top_symbols as scan_equity
            _scanned = await scan_equity(
                broker,
                symbols,
                top_n=int(_scan_cfg.get("top_n", 3)),
                lookback_days=int(_scan_cfg.get("lookback_days", 3)),
                prefer_trending=bool(_scan_cfg.get("prefer_trending", True)),
                inter_request_sleep=float(_scan_cfg.get("inter_request_sleep", 1.5)),
                verbose=True,
            )
            if _scanned:
                print(f"[{name}] equity scanner selected: {_scanned}")
                candidates = _scanned
                try:
                    from scripts.heartbeat import notify_lot_started
                    notify_lot_started(name, _scanned[0], _scanned)
                except Exception:
                    pass
            else:
                print(f"[{name}] equity scanner returned no results — using static symbols")
        except Exception as _scan_e:
            print(f"[{name}] equity scanner error: {_scan_e} — using static symbols")

    # Pre-score: build per-symbol strategy, run against latest CSV bar, rank with PortfolioSelector
    class _DummyRisk:
        """Minimal stand-in for pre-scoring only (hybrid_orb_vwap calls risk.position_size)."""
        def position_size(self, *a, **kw): return 1
        def can_trade_now(self, *a, **kw): return True

    _prescore_candidates: List[Candidate] = []
    _csv_dir = "data"
    _uni_tz = uni.get("timezone", "America/Chicago")

    for _sym in candidates:
        try:
            _ov = _sym_overrides.get(_sym, {})
            _s_raw = {**strat_cfg_raw, **_ov.get("strategy", {})}
            _s_tick = float(_ov.get("tick_size", tick_size))
            _s_pv   = float(_ov.get("point_value", point_value))
            _s_orb  = int(_s_raw.get("orb_minutes", orb_min))
            _s_type = _resolve_strategy_type(uni, _s_raw)
            _s_strat = build_strategy(_s_type, _s_raw, _s_tick, _s_pv)
            _s_win = _norm_windows(_s_raw.get("session_windows") or []) or windows

            _csv = str(Path(_csv_dir) / f"{_symbol_to_csv_stem(_sym)}_live_1m.csv")
            _fdf = load_features_from_csv(_csv, tz=_uni_tz, orb_minutes=_s_orb, symbol=_sym)
            if _fdf is None or _fdf.empty:
                continue

            _bar = _fdf.iloc[-1]
            _dec = _s_strat.maybe_signal(_bar, _s_win, _DummyRisk())
            _score = float(_s_strat.score_signal(_bar, _dec)) if _dec else 0.0
            _prescore_candidates.append(Candidate(symbol=_sym, action=_dec, score=_score, meta={}))
        except Exception:
            pass

    _ranked = PortfolioSelector().choose(_prescore_candidates)
    if _ranked:
        # move winner to front of candidates list
        candidates = [_ranked.symbol] + [s for s in candidates if s != _ranked.symbol]
        print(f"[{name}] pre-score winner: {_ranked.symbol} score={_ranked.score:.3f}")
    else:
        print(f"[{name}] pre-score: no signal – trying candidates in order")

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

    # Apply per-symbol config overrides (crypto_all dict-format: tick_size, strategy, etc.)
    if symbol in _sym_overrides:
        ov = _sym_overrides[symbol]
        if "tick_size" in ov:
            tick_size = float(ov["tick_size"])
        if "point_value" in ov:
            point_value = float(ov["point_value"])
        if "strategy" in ov:
            strat_cfg_raw = {**strat_cfg_raw, **ov["strategy"]}
        orb_min = int(strat_cfg_raw.get("orb_minutes", orb_min))

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
    rb = RollingBars(_uni_tz, orb_min, symbol)

    strategy_type = _resolve_strategy_type(uni, strat_cfg_raw)
    strat = build_strategy(strategy_type, strat_cfg_raw, tick_size, point_value)
    print(f"[{name}] strategy={strategy_type}")

    _risk_uni = uni.get("risk", {})
    risk_cfg = RiskConfig(
        account_equity=float(lot["cash_alloc"]),
        risk_pct=float(lot["max_risk_per_trade"]),
        max_daily_loss_R=float(_risk_uni.get("max_daily_loss_R", 2.0)),
        max_consec_losses=int(_risk_uni.get("max_consec_losses", 3)),
        tick_value=float(point_value),
        flat_time=str(_risk_uni.get("flat_time", "16:00")),
        news_lockout_minutes=0,
    )
    risk = RiskGovernor(risk_cfg)

    # ------------------------------------------------------------------
    # 5b. per-symbol bar log (build our own history)
    # ------------------------------------------------------------------
    print(f"[{name}] CWD={os.getcwd()}")

    bar_log_path = Path("data") / f"{_symbol_to_csv_stem(symbol)}_live_1m.csv"
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
    _pos = {"open": False, "entry_ts": None, "sl_id": None, "tp_id": None,
             "entry_px": 0.0, "stop_px": 0.0, "target_px": 0.0,
             "side": "", "risk_usd": 0.0, "be_done": False}

    # ------------------------------------------------------------------
    # Trade log + notifications (best-effort — never crashes the bot)
    # ------------------------------------------------------------------
    _trades_csv = Path("logs/trades.csv")
    _trades_csv.parent.mkdir(parents=True, exist_ok=True)
    _csv_header = ["ts", "lot", "symbol", "event", "side", "stop", "target", "reason"]
    if not _trades_csv.exists():
        with open(_trades_csv, "w", newline="") as _f:
            csv.writer(_f).writerow(_csv_header)

    def _log_trade(event: str, side: str = "", stop: float = 0.0,
                   target: float = 0.0, reason: str = ""):
        """Append one row to logs/trades.csv and send a Slack message."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = [ts, name, symbol, event, side, f"{stop:.4f}", f"{target:.4f}", reason]
        try:
            with open(_trades_csv, "a", newline="") as _f:
                csv.writer(_f).writerow(row)
        except Exception as _e:
            print(f"[{name}] trade log write error: {_e}")
        try:
            import scripts.notify_slack as _ns
            emoji = "🟢" if event == "ENTRY" else ("🔴" if "STOP" in reason.upper() else "🏁")
            msg = f"{emoji} **{name}** {event} {side} {symbol}"
            if event == "ENTRY":
                msg += f"  stop={stop:.4f}  target={target:.4f}"
            elif reason:
                msg += f"  ({reason})"
            _ns.notify(msg)
        except Exception:
            pass  # no webhook configured — silently skip

        # ── Update lot_status for heartbeat / daily-summary ──────────────────
        if lot_status is not None and name in lot_status:
            ls = lot_status[name]
            if event == "ENTRY":
                ls["trades_today"] += 1
                ls["open"]         = True
                ls["open_symbol"]  = symbol
                ls["open_side"]    = side
            elif event == "EXIT":
                ls["open"]        = False
                ls["open_symbol"] = ""
                ls["open_side"]   = ""
                # Estimate P&L from stored bracket info (R-based approximation)
                ep = _pos.get("entry_px",  0.0)
                sp = _pos.get("stop_px",   0.0)
                tp = _pos.get("target_px", 0.0)
                ru = _pos.get("risk_usd",  0.0)
                if ru > 0 and ep > 0 and sp > 0:
                    if "TARGET" in reason.upper() and tp > 0:
                        d_stop    = abs(sp - ep)
                        d_target  = abs(tp - ep)
                        pnl_est   = (d_target / d_stop * ru) if d_stop > 0 else 0.0
                        ls["targets_today"] += 1
                    else:
                        pnl_est = -ru
                        ls["stops_today"] += 1
                    ls["pnl_today"] += pnl_est

    # Exit params (read once from config so on_bar doesn't re-parse every bar)
    _max_hold_min     = uni.get("execution", {}).get("max_hold_min")
    _be_lock_min      = uni.get("execution", {}).get("be_lock_min")
    _be_req_ticks     = int(uni.get("execution", {}).get("be_req_profit_ticks", 1))
    _exit_on_ema      = bool(strat_cfg_raw.get("exit_on_ema", False))
    _exit_ema_len     = int(strat_cfg_raw.get("exit_ema_len", 20))
    _exit_ema_side    = str(strat_cfg_raw.get("exit_ema_side", "cross"))
    _exit_ema_col     = f"ema_exit_{_exit_ema_len}"
    _regime_skip_bear = bool(strat_cfg_raw.get("regime_skip_bear", False))

    # EOD flat time for IBKR lots (e.g. "15:55" ET) — ignored for crypto
    _flat_time_str  = str(uni.get("risk", {}).get("flat_time", "")) or ""
    _flat_fired     = {"done": False}  # reset each day via date tracking
    _flat_last_date = {"date": None}

    # Orphan cooldown — after a BracketOrphanError, block re-entry for 60 min
    _orphan_until   = {"ts": None}

    async def _force_close(reason: str):
        """Cancel bracket orders and market-sell the open position (CCXT only)."""
        if CcxtBinanceus is None or not isinstance(broker, CcxtBinanceus):
            return
        # Stop the bracket poll so it doesn't fire on_fill after we close
        if hasattr(broker, '_bracket_task') and broker._bracket_task and not broker._bracket_task.done():
            broker._bracket_task.cancel()
        # Cancel both open orders
        for oid, label in [(_pos.get("sl_id"), "stop"), (_pos.get("tp_id"), "target")]:
            if oid:
                try:
                    await broker.exchange.cancel_order(oid, symbol)
                    print(f"[{name}] cancelled {label} order {oid}")
                except Exception as e:
                    print(f"[{name}] cancel {label} {oid} error: {e}")
        # Market sell whatever base-currency balance we hold
        try:
            base = symbol.split('/')[0]  # e.g. "LTC" from "LTC/USDT"
            bal = await broker.exchange.fetch_balance()
            coin_qty = float((bal.get(base) or {}).get('free', 0))
            mkt = broker.exchange.markets.get(symbol, {})
            min_amt = float(((mkt.get('limits') or {}).get('amount') or {}).get('min') or 0)
            if coin_qty > 0 and coin_qty >= min_amt:
                coin_qty = broker.exchange.amount_to_precision(symbol, coin_qty)
                await broker.exchange.create_order(symbol, 'market', 'sell', coin_qty)
                print(f"[{name}] FORCE CLOSE ({reason}): sold {coin_qty} {base}")
                _log_trade("EXIT", reason=f"FORCE:{reason}")
            else:
                print(f"[{name}] FORCE CLOSE ({reason}): no {base} balance to sell ({coin_qty})")
        except Exception as e:
            print(f"[{name}] FORCE CLOSE error: {e} — CHECK BINANCE.US MANUALLY")
        _pos["open"]     = False
        _pos["entry_ts"] = None
        _pos["sl_id"]    = None
        _pos["tp_id"]    = None

    async def on_bar(bar: dict):
        if stop.is_set() or _lot_stop.is_set():
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

        # --- EOD flat for IBKR lots (crypto ignored — flat_time_str is empty) ---
        if _flat_time_str and hasattr(broker, 'flatten_all'):
            try:
                _tz_et = pytz.timezone("America/New_York")
                _now_et = datetime.now(timezone.utc).astimezone(_tz_et)
                _today  = _now_et.date()
                # Reset the fired flag each new calendar day
                if _flat_last_date["date"] != _today:
                    _flat_last_date["date"] = _today
                    _flat_fired["done"]     = False
                if not _flat_fired["done"]:
                    _fh, _fm = map(int, _flat_time_str.split(":"))
                    _flat_dt = _tz_et.localize(dt.datetime.combine(_today, dt.time(_fh, _fm)))
                    if datetime.now(timezone.utc) >= _flat_dt.astimezone(timezone.utc):
                        print(f"[{name}] EOD flat: cancelling orders and flattening positions at {_flat_time_str} ET")
                        _flat_fired["done"] = True
                        _log_trade("EXIT", reason="EOD_FLAT")
                        try:
                            await broker.flatten_all()
                        except Exception as _fe:
                            print(f"[{name}] EOD flatten_all error: {_fe}")
                        _pos["open"]     = False
                        _pos["entry_ts"] = None
                        _pos["sl_id"]    = None
                        _pos["tp_id"]    = None
            except Exception as _eod_e:
                print(f"[{name}] EOD flat check error: {_eod_e}")

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

        if _pos["open"]:
            # --- time-based exit ---
            if _max_hold_min and _pos.get("entry_ts"):
                try:
                    import datetime as _dt2
                    entry_dt  = _dt2.datetime.fromisoformat(str(_pos["entry_ts"]).replace("Z", "+00:00"))
                    cur_dt    = _dt2.datetime.fromisoformat(str(bar_norm["datetime"]).replace("Z", "+00:00"))
                    elapsed_m = (cur_dt - entry_dt).total_seconds() / 60.0
                    if elapsed_m >= _max_hold_min:
                        await _force_close(f"max_hold_min={_max_hold_min} elapsed={elapsed_m:.0f}m")
                        return
                except Exception as _e:
                    print(f"[{name}] max_hold_min parse error: {_e}")
            # --- breakeven lock: after be_lock_min minutes, market-sell if in profit --------
            # For LTC/XRP: be_lock_min=14, be_req_profit_ticks=1 (from execution config)
            # For ETH/SOL: be_lock_min=999999 → effectively disabled
            if (_be_lock_min and _be_lock_min < 999998
                    and not _pos.get("be_done")
                    and _pos.get("entry_ts") and _pos.get("entry_px")):
                try:
                    import datetime as _dt2
                    entry_dt   = _dt2.datetime.fromisoformat(str(_pos["entry_ts"]).replace("Z", "+00:00"))
                    cur_dt     = _dt2.datetime.fromisoformat(str(bar_norm["datetime"]).replace("Z", "+00:00"))
                    elapsed_m  = (cur_dt - entry_dt).total_seconds() / 60.0
                    if elapsed_m >= _be_lock_min:
                        direction    = 1.0 if _pos.get("side", "BUY").upper() == "BUY" else -1.0
                        profit_ticks = (float(latest["close"]) - _pos["entry_px"]) * direction / tick_size
                        if profit_ticks >= _be_req_ticks:
                            _pos["be_done"] = True
                            await _force_close(
                                f"be_lock(t={elapsed_m:.0f}m,{profit_ticks:.1f}tck)"
                            )
                            return
                except Exception as _be_e:
                    print(f"[{name}] be_lock check error: {_be_e}")
            # --- EMA touch/cross exit (for long mean-reversion positions) ---
            if _exit_on_ema and _exit_ema_col in latest.index:
                try:
                    ema_val   = float(latest[_exit_ema_col])
                    close_val = float(latest["close"])
                    hit = (close_val >= ema_val) if _exit_ema_side == "touch" else (close_val > ema_val)
                    if hit:
                        await _force_close(
                            f"exit_on_ema({_exit_ema_side}) close={close_val:.4f} ema={ema_val:.4f}"
                        )
                        return
                except Exception as _e:
                    print(f"[{name}] exit_on_ema check error: {_e}")
            return  # still in position, no exit triggered

        # Orphan cooldown — skip entry if a recent BracketOrphanError occurred
        if _orphan_until["ts"] and datetime.now(timezone.utc) < _orphan_until["ts"]:
            return

        # Skip order placement on historical/backfill bars.
        # During backfill, 1000s of bars are emitted at full speed; any signal that
        # fires triggers a real buy → bracket fails (coins not settled) → emergency close
        # → _pos stays False → next bar fires again → fee-burning loop.
        # Guard: if the bar's close timestamp is more than 5 minutes old, it's historical.
        try:
            _bar_dt = datetime.fromisoformat(bar_norm["datetime"].replace("Z", "+00:00"))
            if _bar_dt.tzinfo is None:
                _bar_dt = _bar_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - _bar_dt) > timedelta(minutes=5):
                return  # historical bar — let indicators build, no live orders
        except Exception:
            pass  # if datetime parse fails, proceed normally

        # Regime filter: skip BUY entries when price is below the 200-bar trend EMA
        # Enable per-universe with `regime_skip_bear: true` in the strategy: block of the YAML
        if _regime_skip_bear and "ema_200" in latest.index:
            if order.side.upper() == "BUY" and float(latest["close"]) < float(latest["ema_200"]):
                return  # bear trend guard — suppress mean-reversion buy

        state.trade_count += 1
        state.last_decision_ts = now_iso
        state.last_side = order.side
        state.last_qty = float(order.qty)
        state.last_stop = float(bracket.stop_price)
        state.last_target = float(bracket.target_price)
        state.symbol = symbol
        state.save(state_path)

        if dry_run:
            entry_approx = float(bar_norm.get("close", 0))
            _side_emoji  = "📈" if order.side.upper() == "BUY" else "📉"
            _dry_msg = (
                f"{_side_emoji} **PAPER SIGNAL [{name}]**\n"
                f"`{order.side}` **{order.symbol}** @ ~${entry_approx:.2f}\n"
                f"Stop: **${bracket.stop_price:.2f}** | Target: **${bracket.target_price:.2f}**\n"
                f"_(Paper only — enter manually if desired, exit at stop/target)_"
            )
            print(f"[{name}] DRY-RUN signal {order.side} {order.qty} {order.symbol} "
                f"entry~={entry_approx:.2f} stop={bracket.stop_price:.4f} target={bracket.target_price:.4f}")
            try:
                import scripts.notify_slack as _ns
                _ns.notify(_dry_msg)
            except Exception:
                pass
            # Count dry-run signals in lot_status so the heartbeat shows activity
            if lot_status is not None and name in lot_status:
                lot_status[name]["trades_today"] += 1
            return

        tif = "DAY" if lot["broker"].startswith("ibkr") else None
        outside_rth = False if lot["broker"].startswith("ibkr") else None

        # For CCXT: qty=1 from strategy is a placeholder; replace with risk-based USD notional.
        # place_bracket_limit expects qty in USD (it divides by price to get coin amount).
        _use_ccxt_maker = False
        if CcxtBinanceus is not None and isinstance(broker, CcxtBinanceus):
            cash_alloc = float(lot.get("cash_alloc", 100))
            _last_close = float(latest["close"])
            stop_dist = abs(float(bracket.stop_price) - _last_close)
            if stop_dist > 0:
                risk_usd = cash_alloc * float(lot.get("max_risk_per_trade", 0.0075))
                order_qty = risk_usd * _last_close / stop_dist
            else:
                order_qty = cash_alloc * 0.5
            # hard cap: never spend more than the lot's cash allocation in one trade
            order_qty = min(order_qty, cash_alloc)

            # Maker limit price: 1 tick inside the spread so the order rests as a
            # post-only maker (4 bps fee vs 10 bps taker).  place_bracket_limit will
            # fall back to a market order automatically if not filled within 45s.
            _maker_px = (
                _last_close - tick_size if order.side.upper() == "BUY"
                else _last_close + tick_size
            )
            _stop_dist   = abs(float(bracket.stop_price)  - _last_close)
            _target_dist = abs(float(bracket.target_price) - _last_close)
            if order.side.upper() == "BUY":
                _adj_stop   = _maker_px - _stop_dist
                _adj_target = _maker_px + _target_dist
            else:
                _adj_stop   = _maker_px + _stop_dist
                _adj_target = _maker_px - _target_dist
            _use_ccxt_maker = True
        else:
            order_qty = int(order.qty)

        try:
            if _use_ccxt_maker:
                # Limit order (post-only) → maker fee; falls back to market on timeout
                trades = await broker.place_bracket_limit(
                    side=order.side,
                    qty=order_qty,
                    limit_price=_maker_px,
                    stop=_adj_stop,
                    target=_adj_target,
                    tif=tif,
                    outsideRth=outside_rth,
                )
            else:
                trades = await broker.place_bracket_market(
                    side=order.side,
                    qty=order_qty,
                    stop_price=float(bracket.stop_price),
                    target_price=float(bracket.target_price),
                    tif=tif,
                    outsideRth=outside_rth,
                )
            # trades: [0]=market parent, [1]=stop_loss, [2]=target limit
            sl_id = str(trades[1].order.orderId)
            tp_id = str(trades[2].order.orderId)
            _pos["open"]      = True
            _pos["entry_ts"]  = bar_norm["datetime"]
            _pos["sl_id"]     = sl_id
            _pos["tp_id"]     = tp_id
            _pos["entry_px"]  = _maker_px if _use_ccxt_maker else float(latest["close"])
            _pos["stop_px"]   = _adj_stop   if _use_ccxt_maker else float(bracket.stop_price)
            _pos["target_px"] = _adj_target if _use_ccxt_maker else float(bracket.target_price)
            _pos["side"]      = order.side
            _pos["risk_usd"]  = (
                float(lot.get("cash_alloc", 100)) * float(lot.get("max_risk_per_trade", 0.0075))
                if CcxtBinanceus is not None and isinstance(broker, CcxtBinanceus)
                else 0.0
            )
            _pos["be_done"]   = False   # reset BE lock for this new trade
            print(f"[{name}] submitted bracket for {order.symbol} sl={sl_id} tp={tp_id}")
            _log_trade("ENTRY", side=order.side,
                       stop=_adj_stop   if _use_ccxt_maker else float(bracket.stop_price),
                       target=_adj_target if _use_ccxt_maker else float(bracket.target_price))
            # Tell the strategy an entry was submitted so min_minutes_between_trades works
            if hasattr(strat, 'on_entry_submitted'):
                try:
                    import pandas as _pd
                    _ts = _pd.Timestamp(bar_norm["datetime"])
                    if _ts.tzinfo is None:
                        _ts = _ts.tz_localize('UTC')
                    strat.on_entry_submitted(_ts)
                except Exception as _oe:
                    print(f"[{name}] on_entry_submitted error: {_oe}")

            # CCXT: start background OCA monitor (cancels loser when winner fills)
            if hasattr(broker, 'watch_bracket'):
                def _on_fill(filled_id):
                    reason = "TARGET" if filled_id == tp_id else "STOP"
                    print(f"[{name}] bracket closed: {reason}")
                    _log_trade("EXIT", reason=reason)
                    _pos["open"]     = False
                    _pos["entry_ts"] = None
                    _pos["sl_id"]    = None
                    _pos["tp_id"]    = None
                broker.watch_bracket(sl_id, tp_id, on_fill=_on_fill)

        except BracketOrphanError as e:
            # Market buy succeeded but bracket + emergency close both failed.
            # Lock the lot to prevent re-entry — manual close required on Binance.US.
            print(f"[{name}] CRITICAL: orphaned position — {e}")
            print(f"[{name}] Lot LOCKED until manual close — sell {symbol} on Binance.US then restart")
            _pos["open"]     = True
            _pos["entry_ts"] = bar_norm["datetime"]
            _pos["sl_id"]    = None
            _pos["tp_id"]    = None
            # Set cooldown so even after force-close, lot waits 60 min before re-entering
            _orphan_until["ts"] = datetime.now(timezone.utc).replace(
                second=0, microsecond=0
            ) + __import__('datetime').timedelta(minutes=60)

        except Exception as e:
            print(f"[{name}] ERROR placing bracket: {e}")
            # Even though the bracket failed (emergency close fired), call on_entry_submitted
            # so the strategy's min_minutes_between_trades cooldown engages.
            # Without this, the strategy fires again on the very next bar, burning fees in a loop.
            if hasattr(strat, 'on_entry_submitted'):
                try:
                    import pandas as _pd
                    _ts_err = _pd.Timestamp.now(tz='UTC')
                    strat.on_entry_submitted(_ts_err)
                except Exception:
                    pass


    # ------------------------------------------------------------------
    # 7. crash-recovery: restore _pos from any open bracket on Binance.US
    # ------------------------------------------------------------------
    if CcxtBinanceus is not None and isinstance(broker, CcxtBinanceus) and not dry_run:
        try:
            open_orders = await broker.exchange.fetch_open_orders(symbol)
            stop_orders   = [o for o in open_orders if o.get('type') in ('stop_loss_limit', 'stop', 'stop_loss')]
            target_orders = [o for o in open_orders if o.get('type') == 'limit']
            if stop_orders and target_orders:
                recovered_sl = str(stop_orders[0]['id'])
                recovered_tp = str(target_orders[0]['id'])
                _pos["open"]     = True
                _pos["sl_id"]    = recovered_sl
                _pos["tp_id"]    = recovered_tp
                # Use current time so max_hold_min countdown starts fresh after restart
                _pos["entry_ts"] = datetime.now(timezone.utc).isoformat()
                print(f"[{name}] RECOVERY: open bracket found — sl={recovered_sl} tp={recovered_tp} — resuming watch")
                if hasattr(broker, 'watch_bracket'):
                    def _on_fill_recovered(filled_id):
                        reason = "TARGET" if filled_id == _pos["tp_id"] else "STOP"
                        print(f"[{name}] recovered bracket closed: {reason}")
                        _pos["open"]     = False
                        _pos["entry_ts"] = None
                        _pos["sl_id"]    = None
                        _pos["tp_id"]    = None
                    broker.watch_bracket(recovered_sl, recovered_tp, on_fill=_on_fill_recovered)
            elif open_orders:
                print(f"[{name}] RECOVERY: {len(open_orders)} open order(s) found but can't match stop+target "
                      f"— check Binance.US manually. Types: {[o.get('type') for o in open_orders]}")
                # Lock the lot so we don't re-enter while orphaned orders exist
                _pos["open"]     = True
                _pos["entry_ts"] = datetime.now(timezone.utc).isoformat()
                _pos["sl_id"]    = None
                _pos["tp_id"]    = None
                _log_trade("RECOVERY_LOCKED", reason="unmatched_orders")
            else:
                # No open orders — but check if we're holding coins without protection
                try:
                    base = symbol.split('/')[0]
                    bal = await broker.exchange.fetch_balance()
                    coin_qty = float((bal.get(base) or {}).get('total', 0))
                    mkt = broker.exchange.markets.get(symbol, {})
                    min_amt = float(((mkt.get('limits') or {}).get('amount') or {}).get('min') or 0)
                    cash_alloc = float(lot.get("cash_alloc", 100))
                    ticker = await broker.exchange.fetch_ticker(symbol)
                    coin_usd = coin_qty * float(ticker['last'])
                    # If holding coins worth more than 10% of allocation — something is wrong
                    if coin_qty > min_amt and coin_usd > cash_alloc * 0.10:
                        print(f"[{name}] RECOVERY: UNPROTECTED position detected — "
                              f"holding {coin_qty:.6f} {base} (~${coin_usd:.2f}) with no open orders!")
                        print(f"[{name}] Lot LOCKED — sell {base} on Binance.US then restart")
                        _pos["open"]     = True
                        _pos["entry_ts"] = datetime.now(timezone.utc).isoformat()
                        _pos["sl_id"]    = None
                        _pos["tp_id"]    = None
                        _log_trade("RECOVERY_LOCKED", reason=f"unprotected_coins_{coin_qty:.6f}{base}")
                    else:
                        print(f"[{name}] RECOVERY: no open orders on {symbol} — clean startup")
                except Exception as _be:
                    print(f"[{name}] RECOVERY: balance check failed: {_be} — assuming clean startup")
        except Exception as e:
            print(f"[{name}] RECOVERY check failed: {e} — assuming clean startup")

    # ------------------------------------------------------------------
    # 8. start stream + wait
    # ------------------------------------------------------------------
    def _task_done(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            print(f"[{name}] bar_task crashed: {type(exc).__name__}: {exc}")
        else:
            print(f"[{name}] bar_task ended cleanly")



    # ------------------------------------------------------------------
    # 7b. Periodic re-scan task (scan_universe lots only)
    # When the scanner finds a better symbol and the lot is flat, sets
    # _lot_stop so this lot exits cleanly, then self-restarts via a new
    # asyncio task — picking the new top candidate on the next startup scan.
    # ------------------------------------------------------------------
    _rescan_task = None
    if _scan_universe and CcxtBinanceus is not None and isinstance(broker, CcxtBinanceus):
        _rescan_interval_min = int(_scan_cfg.get("rescan_interval_min", 60))
        if _rescan_interval_min > 0:
            async def _rescan_fn():
                await asyncio.sleep(_rescan_interval_min * 60)
                while not stop.is_set() and not _lot_stop.is_set():
                    if not _pos["open"]:
                        try:
                            from trader.engine.crypto_scanner import scan_top_symbols as _scan_fn
                            _new = await _scan_fn(
                                broker.exchange,
                                top_n=int(_scan_cfg.get("top_n", 5)),
                                min_volume_usd=float(_scan_cfg.get("min_volume_usd", 100_000)),
                                min_atr_pct=float(_scan_cfg.get("min_atr_pct", 0.003)),
                                prefer_trending=bool(_scan_cfg.get("prefer_trending", True)),
                                verbose=False,
                            )
                            if _new and _new[0] != symbol:
                                print(f"[{name}] RESCAN: new top coin {_new[0]} (was {symbol}) — rotating")
                                try:
                                    from scripts.heartbeat import notify_rotation
                                    notify_rotation(name, symbol, _new[0])
                                except Exception:
                                    pass
                                _lot_stop.set()
                                return
                            else:
                                top = _new[0] if _new else "none"
                                print(f"[{name}] RESCAN: top={top} unchanged, keeping {symbol}")
                        except Exception as _re:
                            print(f"[{name}] rescan error: {_re}")
                    else:
                        print(f"[{name}] RESCAN: position open, deferring rotation check")
                    await asyncio.sleep(_rescan_interval_min * 60)
            _rescan_task = asyncio.create_task(_rescan_fn())

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
        # run until global shutdown OR lot-local rotation trigger
        while not stop.is_set() and not _lot_stop.is_set():
            await asyncio.sleep(0.2)

    except asyncio.CancelledError:
        pass
    finally:
        if _rescan_task:
            _rescan_task.cancel()
            with suppress(Exception):
                await _rescan_task
        bar_task.cancel()
        watch_task.cancel()
        with suppress(Exception):
            await bar_task
        with suppress(Exception):
            await watch_task
        await broker.disconnect()
        print(f"[{name}] microlot stopped and broker disconnected")

    # Symbol rotation: if _lot_stop fired (not a global shutdown), self-restart so the
    # startup scanner picks the new top coin.
    if _lot_stop.is_set() and not stop.is_set():
        print(f"[{name}] rotation: restarting microlot to pick new top symbol...")
        asyncio.get_event_loop().create_task(
            run_microlot(lot, dry_run, client_id, stop_all, lot_status),
            name=f"microlot:{name}",
        )


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
