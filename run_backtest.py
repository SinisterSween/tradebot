# run_backtest.py
import argparse
import os
from pathlib import Path
from collections import Counter
from datetime import date
import yaml
import pandas as pd
import matplotlib

from trader.engine.backtest import prepare_bars, equity_curve
from trader.engine.execution import OrderManager
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig as OrbCfg
from trader.strategies.rsi_mean_reversion import RsiMeanReversion, StratConfig as RsiMRConfig
from trader.strategies.vwap_reversion import VwapReversion, StratConfig as RevCfg
from trader.strategies.trend_pullback import TrendPullback, StratConfig as PbCfg
from trader.engine.latency import BarDelay
from trader.engine.metrics_ext import summarize_equity, write_artifacts

def _in_windows(ts_local_str, windows):
    try:
        hh, mm = map(int, str(ts_local_str)[:5].split(":"))
    except Exception:
        return False
    cur = hh * 60 + mm
    for w in windows:
        s_h, s_m = map(int, str(w["start"]).split(":"))
        e_h, e_m = map(int, str(w["end"]).split(":"))
        if (s_h * 60 + s_m) <= cur <= (e_h * 60 + e_m):
            return True
    return False

def _pick_microlot(portfolio_cfg: dict, microlot_name: str) -> dict:
    lots = portfolio_cfg.get("microlots") or []
    want = (microlot_name or "").strip()
    for m in lots:
        if str(m.get("name", "")).strip() == want:
            return m
    raise SystemExit(f"Microlot not found: {microlot_name}. Options: {[m.get('name') for m in lots]}")

def _symbol_to_csv_stem(symbol: str) -> str:
    # Matches feeder naming: "BTC/USDT" -> "BTC_USDT", "AAPL" -> "AAPL"
    return str(symbol).upper().replace("/", "_")

def _resolve_universe_path(universe_name_or_path: str) -> str:
    # allow passing "sp500_fractional" or "config/universes/sp500_fractional.yaml"
    s = str(universe_name_or_path)
    if s.endswith(".yaml") and os.path.exists(s):
        return s
    p = os.path.join("config", "universes", f"{s}.yaml")
    return p


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
    cur = hh * 60 + mm
    for w in windows:
        s_h, s_m = map(int, str(w["start"]).split(":"))
        e_h, e_m = map(int, str(w["end"]).split(":"))
        if (s_h * 60 + s_m) <= cur <= (e_h * 60 + e_m):
            return (e_h * 60 + e_m) - cur
    return 9999

from dataclasses import fields, is_dataclass

def _norm_strat_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "HybridOrbVwap"

    key = s.replace("-", "_").replace(" ", "_").lower()

    aliases = {
        # ORB
        "hybrid_orb_vwap": "HybridOrbVwap",
        "hybridorbvwap": "HybridOrbVwap",
        "orb_vwap": "HybridOrbVwap",

        # Reversion
        "vwap_reversion": "VwapReversion",
        "vwapreversion": "VwapReversion",

        # Pullback
        "trend_pullback": "TrendPullback",
        "trendpullback": "TrendPullback",

        # RSI Mean Reversion
        "rsi_mean_reversion": "RsiMeanReversion",
        "rsimeanreversion": "RsiMeanReversion",
    }

    return aliases.get(key, s)  # if already "VwapReversion", keep it


def _filter_kwargs(dataclass_type, kwargs: dict) -> dict:
    """Return only kwargs that exist on a dataclass."""
    if not is_dataclass(dataclass_type):
        return kwargs
    allowed = {f.name for f in fields(dataclass_type)}
    return {k: v for k, v in kwargs.items() if k in allowed}


def build_strategy(strategy_name: str, s_cfg: dict, fees: dict):
    name = _norm_strat_name(strategy_name or "HybridOrbVwap")

    if name == "VwapReversion":
        rev_kwargs = dict(
            atr_len=int(s_cfg.get("atr_len", 14)),
            atr_mult=float(s_cfg.get("atr_mult", 1.0)),
            target_R=float(s_cfg.get("target_R", 1.0)),
            slope_max=float(s_cfg.get("slope_max", 0.03)),
            stop_pad_ticks=int(s_cfg.get("stop_pad_ticks", 2)),
            tick_size=float(fees["tick_size"]),
            tick_value=float(fees["tick_value"]),
            target_vwap=bool(s_cfg.get("target_vwap", True)),
            min_atr_pct=float(s_cfg.get("min_atr_pct", 0.0)),
            max_bar_range_atr=float(s_cfg.get("max_bar_range_atr", 0.0)),
            ema_slope_max=float(s_cfg.get("ema_slope_max", 0.0)),
            ema_max_dist_atr=float(s_cfg.get("ema_max_dist_atr", 0.0)),

        )
        cfg = RevCfg(**_filter_kwargs(RevCfg, rev_kwargs))
        return VwapReversion(cfg)

    if name == "TrendPullback":
        pb_kwargs = dict(
            atr_len=int(s_cfg.get("atr_len", 14)),
            atr_mult=float(s_cfg.get("atr_mult", 1.0)),
            target_R=float(s_cfg.get("target_R", 1.0)),
            slope_min=float(s_cfg.get("slope_min", 0.08)),
            pullback_atr=float(s_cfg.get("pullback_atr", 0.35)),
            stop_pad_ticks=int(s_cfg.get("stop_pad_ticks", 2)),
            tick_size=float(fees["tick_size"]),
            tick_value=float(fees["tick_value"]),
            min_atr_pct=float(s_cfg.get("min_atr_pct", 0.0)),
            max_bar_range_atr=float(s_cfg.get("max_bar_range_atr", 0.0)),
        )
        cfg = PbCfg(**_filter_kwargs(PbCfg, pb_kwargs))
        return TrendPullback(cfg)
    
    if name == "RsiMeanReversion":
        pb_kwargs = dict(
            rsi_len=int(s_cfg.get("rsi_len", 14)),
            rsi_buy_below=float(s_cfg.get("rsi_buy_below", 28)),
            rsi_sell_above=float(s_cfg.get("rsi_sell_above", 72)),
            atr_mult=float(s_cfg.get("atr_mult", 1.0)),
            target_R=float(s_cfg.get("target_R", s_cfg.get("take_profit_R", 0.8))),
            stop_pad_ticks=int(s_cfg.get("stop_pad_ticks", 2)),
            slope_max=float(s_cfg.get("slope_max", 0.0)),
            min_atr_pct=float(s_cfg.get("min_atr_pct", 0.0)),
            target_vwap=bool(s_cfg.get("target_vwap", False)),
            tick_size=float(fees.get("tick_size", 0.01)),
            tick_value=float(fees.get("tick_value", 1.0)),
        )
        cfg = RsiMRConfig(**_filter_kwargs(RsiMRConfig, pb_kwargs))
        return RsiMeanReversion(cfg)


    # default: HybridOrbVwap
    orb_kwargs = dict(
        orb_minutes=int(s_cfg.get("orb_minutes", 5)),
        atr_len=int(s_cfg.get("atr_len", 14)),
        atr_mult=float(s_cfg.get("atr_mult", 1.0)),
        target_R=float(s_cfg.get("target_R", 1.0)),
        slope_min=float(s_cfg.get("slope_min", 0.0)),
        break_eps_ticks=int(s_cfg.get("break_eps_ticks", 2)),
        stop_pad_ticks=int(s_cfg.get("stop_pad_ticks", 2)),
        trail_pad_ticks=int(s_cfg.get("trail_pad_ticks", 2)),
        tick_size=float(fees["tick_size"]),
        tick_value=float(fees["tick_value"]),
    )
    cfg = OrbCfg(**_filter_kwargs(OrbCfg, orb_kwargs))
    return HybridOrbVwap(cfg)

def _minutes_between(a_str, b_str):
    try:
        ah, am = map(int, str(a_str)[:5].split(":"))
        bh, bm = map(int, str(b_str)[:5].split(":"))
        return (bh * 60 + bm) - (ah * 60 + am)
    except Exception:
        return 0

def _is_crypto_like(universe_cfg: dict | None) -> bool:
    try:
        return str((universe_cfg or {}).get("asset_class", "")).lower() == "crypto"
    except Exception:
        return False

def _is_equity_like(ct: dict, cfg: dict) -> bool:
    # If profile explicitly says equities, treat as equity
    prof = (cfg.get("profile_name") or "").lower()
    if "equities" in prof:
        return True

    # Heuristic: SMART + tick_size 0.01 + dollars-per-point = 1/share
    try:
        exch = str(ct.get("exchange", "")).upper()
        tick_size = float(ct.get("tick_size", 0.0))
        tick_value = float(ct.get("tick_value", 0.0))
        dpp = tick_value / tick_size if tick_size else 0.0
        if exch == "SMART" and abs(tick_size - 0.01) < 1e-9 and abs(dpp - 1.0) < 1e-9:
            return True
    except Exception:
        pass

    return False


import math

def _floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step

def _compute_size_crypto(
    *, equity: float, risk_pct: float,
    entry_px: float, stop_px: float,
    max_notional_usd: float | None = None,
    trade_min_qty: float = 0.0,
    max_qty: float | None = None,
) -> float:
    # Risk budget in USD
    risk_budget = float(equity) * float(risk_pct)

    # USD risk per 1 unit of coin
    risk_per_unit = abs(float(entry_px) - float(stop_px))
    if risk_per_unit <= 0:
        return 0.0

    qty_by_risk = risk_budget / risk_per_unit

    # affordability in notional terms (still matters)
    qty_by_cash = float(equity) / float(entry_px) if entry_px > 0 else 0.0

    qty = min(qty_by_risk, qty_by_cash)

    if max_qty is not None:
        qty = min(qty, float(max_qty))
    
    qty_by_cash = (max_notional_usd / entry_px) if (max_notional_usd is not None and entry_px > 0) else (equity / entry_px)
    qty = min(qty_by_risk, qty_by_cash)

    # enforce minimum tradable qty (your yaml)
    qty = _floor_to_step(qty, float(trade_min_qty)) if trade_min_qty > 0 else qty
    if trade_min_qty and qty < trade_min_qty:
        return 0.0

    return float(qty)



def _compute_size(symbol, equity, risk_pct, stop_ticks, tick_value, last_price, max_size, equity_like: bool,
                  entry_px: float | None = None, stop_px: float | None = None,crypto_like: bool = False,
                  trade_min_qty: float = 0.0,):

    if last_price <= 0:
        return 0

    affordable = int(equity // last_price)
    if affordable <= 0:
        return 0

    # Equities/ETFs: size by risk dollars and stop distance
    if equity_like:
        if not entry_px or not stop_px:
            return 0
        risk_budget = equity * risk_pct
        risk_per_share = abs(entry_px - stop_px)
        if risk_per_share <= 0:
            return 0
        shares_risk = int(risk_budget // risk_per_share)
        shares_affordable = int(equity // entry_px) if entry_px > 0 else 0
        return max(0, min(shares_risk, shares_affordable, max_size))

    # Futures sizing (unchanged)
    risk_capital = equity * risk_pct
    if not stop_ticks or stop_ticks <= 0 or not tick_value:
        return max(0, min(1, max_size))

    risk_per_contract = stop_ticks * tick_value
    if risk_per_contract <= 0:
        return max(0, min(1, max_size))

    raw = int(risk_capital // risk_per_contract)
    return max(0, min(raw, max_size))




def _load_yaml_if_exists(path: str | None) -> dict:
    if not path:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _resolve_contract_for_symbol(symbol: str, contracts_path: str, universe: dict | None) -> dict:
    contracts = _load_yaml_if_exists(contracts_path)
    sym_u = symbol.upper()

    # Prefer explicit contract definition
    if sym_u in contracts:
        return dict(contracts[sym_u] or {})

    # Otherwise build an equity-like contract from universe defaults
    uni = universe or {}
    tick_size = float(uni.get("tick_size", 0.01))
    point_value = float(uni.get("point_value", 1.0))

    # For equities we want dollars-per-point to be 1 (per share).
    # Our engine uses dpp = tick_value / tick_size, so set tick_value = tick_size * 1 => dpp=1.
    return {
        "symbol": sym_u,
        "exchange": "SMART",
        "currency": "USD",
        "tick_size": tick_size,
        "tick_value": float(tick_size) * 1.0,
        # Use a sane default if not given elsewhere
        "commission_per_contract": 0.005,
        "exchange_fees_per_contract": 0.0,
        # keep universe point_value around as an extra field if you want later
        "_point_value": point_value,
    }

def _resolve_strategy_name(*, cfg: dict, universe_cfg: dict | None, symbol: str) -> str:
    """
    Resolve strategy.name for THIS symbol, with explicit precedence:
      1) per-symbol override strategy.name
      2) universe strategy.name
      3) cfg strategy_name (legacy)
      4) default HybridOrbVwap
    """
    sym = str(symbol)
    sym_u = sym.upper()

    # Per-symbol overrides
    ov = None
    if universe_cfg:
        overrides = (universe_cfg.get("symbol_overrides") or {})
        ov = overrides.get(sym) or overrides.get(sym_u)

    if ov:
        nm = ((ov.get("strategy") or {}).get("name") or "").strip()
        if nm:
            return nm

    # Universe-level default
    if universe_cfg:
        nm = ((universe_cfg.get("strategy") or {}).get("name") or "").strip()
        if nm:
            return nm

    # Legacy / misc
    nm = (cfg.get("strategy_name") or "").strip()
    if nm:
        return nm

    return "HybridOrbVwap"


def run_one_symbol(*, args, symbol: str, csv_path: str, universe_cfg: dict | None) -> dict:
    # ---- Load layered config: profile -> env -> config(extra) ----
    cfg = {}
    cfg = _deep_merge(cfg, _load_yaml_if_exists(args.profile))
    cfg = _deep_merge(cfg, _load_yaml_if_exists(args.env))
    cfg = _deep_merge(cfg, _load_yaml_if_exists(args.config))

    # Stash profile name for equity heuristic
    cfg["profile_name"] = os.path.basename(args.profile or "")

    # Merge universe strategy over profile strategy (universe is closer to “intent”)
    if universe_cfg:
        uc = dict(universe_cfg)
        uc.pop("symbols", None)  # don't let symbols pollute cfg
        cfg = _deep_merge(cfg, uc)
        if "strategy" in universe_cfg:
            cfg["strategy"] = _deep_merge(cfg.get("strategy", {}), universe_cfg.get("strategy", {}))
        # Universe session windows if present, else keep profile’s
        uni_windows = (universe_cfg.get("strategy") or {}).get("session_windows")
        if uni_windows:
            cfg.setdefault("strategy", {})
            cfg["strategy"]["session_windows"] = uni_windows
        # Merge universe execution over profile execution (if present)
    if universe_cfg and "execution" in universe_cfg:
        cfg["execution"] = _deep_merge(cfg.get("execution", {}), universe_cfg.get("execution", {}))

    # --- Capture universe/base strategy as fallback BEFORE per-symbol overrides ---
    import copy
    base_strategy_cfg = copy.deepcopy(cfg.get("strategy") or {})
    # base_strategy_cfg = dict((cfg.get("strategy") or {}))
    base_strategy_name = (base_strategy_cfg.get("name") or "").strip()


    # ✅ Per-symbol overrides (NEW)
    sym_key = str(symbol).upper()  # keeps BTC/USDT format intact, normalizes case
    overrides = ((universe_cfg or {}).get("symbol_overrides") or {})
    sym_ovr = overrides.get(symbol) or overrides.get(sym_key)

    if sym_ovr:
        if "strategy" in sym_ovr:
            cfg["strategy"] = _deep_merge(cfg.get("strategy", {}), sym_ovr["strategy"])
        if "execution" in sym_ovr:
            cfg["execution"] = _deep_merge(cfg.get("execution", {}), sym_ovr["execution"])
        # optional if you ever add per-symbol fees
        if "fees" in sym_ovr:
            cfg["fees"] = _deep_merge(cfg.get("fees", {}), sym_ovr["fees"])


    # ---- Contract / fees ----
    ct = _resolve_contract_for_symbol(symbol, args.contracts, universe_cfg)
    cfg.setdefault("fees", {})
    for k in ("tick_size", "tick_value", "commission_per_contract", "exchange_fees_per_contract"):
        if k in ct:
            cfg["fees"][k] = ct[k]
    cfg["symbol"] = ct.get("symbol", symbol)

    if not csv_path or not os.path.exists(csv_path):
        return {
            "Symbol": symbol,
            "CSV": csv_path,
            "Status": "MISSING_CSV",
        }

    # ---- Prep bars ----
    tz = cfg.get("timezone", "America/Chicago")
    df = pd.read_csv(csv_path)
    orb_minutes = int(cfg["strategy"].get("orb_minutes", 5))

    df = prepare_bars(
        df,
        tz=tz,
        orb_minutes=orb_minutes,
        ema_fast_len=int(cfg["strategy"].get("ema_fast_len", 50)),
        ema_slope_lookback=int(cfg["strategy"].get("ema_slope_lookback", 5)),
    )

    # ---- DEBUG: timestamp sanity (do this BEFORE any filtering/sorting) ----
    print(f"[TSCHK] {symbol} index[:3] = {list(df.index[:3])}")
    print(f"[TSCHK] {symbol} t_local head3 = {df['t_local'].head(3).tolist()}")
    print(f"[TSCHK] {symbol} t_local tail3 = {df['t_local'].tail(3).tolist()}")
    print(f"[TSCHK] {symbol} date head/tail = {df['date'].iloc[0]} -> {df['date'].iloc[-1]}")

    #df = prepare_bars(df, tz=tz, orb_minutes=orb_minutes)

    # ---- Date filter (keep df['date'] as datetime.date or Timestamp->date) ----
    if args.start_date:
        start_d = pd.to_datetime(args.start_date).date()
        df = df[df["date"] >= start_d]
    if args.end_date:
        end_d = pd.to_datetime(args.end_date).date()
        df = df[df["date"] <= end_d]


    # # ---- PATCH: enforce chronological order AND preserve timestamp index ----
    # ts_col = next((c for c in ["ts", "datetime", "t_utc", "timestamp", "date_time"] if c in df.columns), None)
    # if ts_col:
    #     df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
    #     df = df.dropna(subset=[ts_col])
    #     df = df.sort_values(ts_col)
    #     df = df.set_index(ts_col, drop=False)   # ✅ bar.name becomes a Timestamp again
    # else:
    #     # fallback: if prepare_bars gave you a DateTimeIndex, just sort it
    #     if not df.index.is_monotonic_increasing:
    #         df = df.sort_index()

    # ensure bars are time-ordered after date filtering
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    # hard-fail if still not monotonic
    if not df.index.is_monotonic_increasing:
        raise ValueError(f"[DATA] {symbol} bars not monotonic after sort")


    # ---- Risk / fees ----
    fees = cfg["fees"]
    yaml_risk = cfg.get("risk", {})

    # Fall back if env/profile didn’t provide risk
    yaml_risk.setdefault("account_equity", args.starting_equity)
    yaml_risk.setdefault("risk_pct", args.risk_pct)
    yaml_risk.setdefault("max_daily_loss_R", 1.5)
    yaml_risk.setdefault("max_consec_losses", 3)
    yaml_risk.setdefault("flat_time", "15:55")
    yaml_risk.setdefault("news_lockout_minutes", 0)

    risk = RiskGovernor(RiskConfig(
        account_equity=float(yaml_risk["account_equity"]),
        risk_pct=float(args.risk_pct or yaml_risk["risk_pct"]),
        max_daily_loss_R=float(yaml_risk["max_daily_loss_R"]),
        max_consec_losses=int(yaml_risk["max_consec_losses"]),
        tick_value=float(fees["tick_value"]),
        flat_time=str(yaml_risk["flat_time"]),
        news_lockout_minutes=int(yaml_risk["news_lockout_minutes"]),
    ))
    risk.risk_buffer_pct = float(cfg.get("risk", {}).get("risk_buffer_pct", 1.0))
    risk.max_contracts_per_trade = int(cfg.get("risk", {}).get("max_contracts_per_trade", 0)) or None

    sc = cfg["strategy"]

    windows = _norm_windows(sc.get("session_windows", []))
    if not windows:
        windows = [{"start": "00:00", "end": "23:59"}]

    # ---- SANITY (risk exists now, so this won’t crash) ----
    tradable = df["t_local"].apply(lambda t: _in_windows(t, windows))
    print(
        f"[SANITY] {symbol} bars={len(df)} tradable={int(tradable.sum())} tradable_pct={float(tradable.mean()) if len(df) else 0.0}"
    )
    print(f"[SANITY] {symbol} index first/last: {df.index[0]} -> {df.index[-1]}")
    print(f"[SANITY] {symbol} tod min/max: {df.index.strftime('%H:%M').min()} -> {df.index.strftime('%H:%M').max()}")

    print(f"[SANITY] {symbol} dates: {df['date'].min()} -> {df['date'].max()}")

    # ---- Order manager / execution ----
    dpp = float(fees["tick_value"]) / float(fees["tick_size"])  # dollars per full point
    om = OrderManager(
        commission_per_contract=float(fees.get("commission_per_contract", 0.0)),
        exchange_fees_per_contract=float(fees.get("exchange_fees_per_contract", 0.0)),
        tick_size=float(fees["tick_size"]),
        fee_bps=float(args.fee_bps),
        fee_fixed=float(args.fee_fixed),
        slip_bps=float(args.slip_bps),
        dollars_per_point=float(dpp),
    )
    delay = BarDelay(bars=int(args.latency_bars or 0))

    # ---- Strategy ----
    print(f"[CFG] {symbol} orb={sc.get('orb_minutes')} atr_mult={sc.get('atr_mult')} slope_min={sc.get('slope_min')}")

    raw_name = (
        (sym_ovr or {}).get("strategy", {}).get("name")
        or (universe_cfg or {}).get("strategy", {}).get("name")
        or cfg.get("strategy_name")
        or "HybridOrbVwap"
    )

    strategy_name = _norm_strat_name(raw_name)

    VALID_STRATS = {"HybridOrbVwap", "VwapReversion", "TrendPullback", "RsiMeanReversion"}
    if strategy_name not in VALID_STRATS:
        raise ValueError(f"[STRATEGY] {symbol}: unknown strategy '{raw_name}' -> '{strategy_name}'. Valid={sorted(VALID_STRATS)}")

    print(f"[STRATEGY] {symbol} -> {strategy_name}")

    strat = build_strategy(strategy_name, cfg["strategy"], fees)

    # ---- Fallback strategy (universe/base) ----
    fallback = None
    fallback_name = None

    # Primary strategy name (already resolved)
    primary_name = strategy_name

    # Fallback strategy name from universe/base (captured before overrides)
    if base_strategy_name:
        fallback_name = _norm_strat_name(base_strategy_name)

    # Only enable fallback if it's different from primary
    if fallback_name and fallback_name != primary_name:
        # Build fallback using base_strategy_cfg (NOT the overridden cfg["strategy"])
        fallback = build_strategy(fallback_name, base_strategy_cfg, fees)
        print(f"[FALLBACK] {symbol} primary={primary_name} fallback={fallback_name}")



    expected_cls = {
        "HybridOrbVwap": HybridOrbVwap,
        "VwapReversion": VwapReversion,
        "TrendPullback": TrendPullback,
        "RsiMeanReversion": RsiMeanReversion,
    }[strategy_name]

    if not isinstance(strat, expected_cls):
        raise TypeError(f"[STRATEGY] {symbol}: expected {expected_cls.__name__}, got {type(strat).__name__}")


    # ---- Execution knobs ----
    exe = cfg.setdefault("execution", {})
    BE_LOCK_MIN      = int(exe.get("be_lock_min", 12))
    BE_REQ_TICKS     = int(exe.get("be_req_profit_ticks", 2))
    MAX_HOLD_MIN     = int(exe.get("max_hold_min", 40))
    HARD_CUTOFF_R    = float(exe.get("hard_cutoff_R", -0.25))
    HARD_CUTOFF_MIN  = int(exe.get("hard_cutoff_min", 10))
    MIN_WIN_END      = int(exe.get("min_minutes_to_window_end", 20))

    print(
        f"[EXEC KNOBS] {symbol} "
        f"BE_LOCK_MIN={BE_LOCK_MIN} BE_REQ_TICKS={BE_REQ_TICKS} "
        f"MAX_HOLD_MIN={MAX_HOLD_MIN} HARD_CUTOFF_R={HARD_CUTOFF_R} "
        f"HARD_CUTOFF_MIN={HARD_CUTOFF_MIN} MIN_WIN_END={MIN_WIN_END}"
    )
    COOLDOWN_BARS = int(exe.get("cooldown_bars", 0) or 0)
    last_flat_i = -10**9
    halted_prev = False
    # ---- Sim loop ----
    equity = float(args.starting_equity)
    open_position = False
    current_bracket = None
    prev_date = None
    open_side = None
    open_qty = 0.0
    open_entry_px = None
    open_entry_ts = None
    open_be_locked = False
    open_risk_pts = None

    why = Counter()

    equity_like = _is_equity_like(ct, cfg)
    crypto_like = _is_crypto_like(universe_cfg)
    trade_min_qty = float((universe_cfg or {}).get("trade_min_qty", 0.0) or 0.0)


    for i, (_, bar) in enumerate(df.iterrows()):
        # new day
        if prev_date is None or bar["date"] != prev_date:
            risk.reset_day(equity + om.realized_pnl)
            prev_date = bar["date"]
            open_position = False
            current_bracket = None
            open_side = None
            open_qty = 0.0
            open_entry_px = None
            open_entry_ts = None
            open_be_locked = False
            open_risk_pts = None

        # latency queue
        for due in delay.due(i):
            if isinstance(due, dict):
                order = due["order"]
                bracket = due.get("bracket")
                entry = float(due.get("entry", getattr(order, "entry", bar["close"])))
                risk_pts_due = due.get("risk_pts")

            else:
                order = due
                bracket = None
                entry = float(getattr(order, "entry", bar["close"]))
                risk_pts_due = None

            if open_position:
                continue

            om.place_and_simulate(bar, order)

            current_bracket = bracket if bracket is not None else current_bracket
            open_position = True
            open_side = str(order.side).upper()
            open_qty = float(order.qty)
            open_entry_px = entry
            open_entry_ts = bar["t_local"]
            open_be_locked = False

            if risk_pts_due is not None:
                open_risk_pts = float(risk_pts_due)
            else:
                open_risk_pts = max(float(fees["tick_size"]), abs(entry - float(current_bracket.stop_price)))

        # outside session => flatten-on-close simulation
        if not _in_windows(bar["t_local"], windows):
            if open_position and current_bracket:
                before = len(om.trades)
                exit_info = om.simulate_bracket(bar, current_bracket)
                exited = bool(exit_info) or (len(om.trades) > before and om.trades[-1].get("action") == "EXIT")

                if not exited:
                    exit_px = float(bar["close"])
                    side = (open_side or "BUY")
                    qty = float(open_qty or getattr(current_bracket, "qty", 0) or 0.0)
                    entry_px = float(open_entry_px if open_entry_px is not None else bar["close"])

                    side_mult = 1.0 if side == "BUY" else -1.0
                    # equities: $ pnl = (exit-entry)*qty ; futures: use dpp
                    if crypto_like or equity_like:
                        pnl = (exit_px - entry_px) * side_mult * qty
                    else:
                        pnl = (exit_px - entry_px) * dpp * side_mult * qty

                    om.trades.append({
                        "ts": bar.get("ts") or bar.get("datetime") or bar.get("t_utc") or None,
                        "t_local": bar["t_local"],
                        "action": "EXIT",
                        "side": side,
                        "qty": float(qty),
                        "price": entry_px,
                        "reason": "WINDOW",
                        "exit_price": exit_px,
                        "pnl": pnl,
                        "fees_total": 0.0,
                    })

                    try:
                        if open_risk_pts and float(open_risk_pts) > 0:
                            pnl_pts = (exit_px - entry_px) * (1.0 if side == "BUY" else -1.0)
                            R = pnl_pts / float(open_risk_pts)
                            risk.record_trade_outcome_R(R)
                            om.trades[-1]["R"] = float(R)
                    except Exception:
                        pass

                    equity += pnl
                    last_flat_i = i

            open_position = False
            current_bracket = None
            open_side = None
            open_qty = 0.0
            open_entry_px = None
            open_entry_ts = None
            open_be_locked = False
            open_risk_pts = None
            continue

        # manage OPEN position
        if open_position and current_bracket:
            before = len(om.trades)
            exit_info = om.simulate_bracket(bar, current_bracket)

            if not exit_info and len(om.trades) > before and om.trades[-1].get("action") == "EXIT":
                last = om.trades[-1]
                exit_info = {
                    "side": last.get("side"),
                    "entry_price": float(last.get("price") or last.get("entry_price") or bar["close"]),
                    "exit_price": float(last.get("exit_price") or bar["close"]),
                    "reason": last.get("reason"),
                }

            if exit_info:
                tick_size = float(fees["tick_size"])
                pnl_pts = float(exit_info["exit_price"]) - float(exit_info["entry_price"])
                risk_pts = abs(float(exit_info["entry_price"]) - float(current_bracket.stop_price))
                if risk_pts < (tick_size / 2.0):
                    risk_pts = tick_size
                side_mult = 1.0 if str(exit_info["side"]).upper() == "BUY" else -1.0
                R = (pnl_pts / risk_pts) * side_mult
                risk.record_trade_outcome_R(R)

                if om.trades:
                    last_exit = om.trades[-1]
                    pnl_usd = float(last_exit.get("pnl", 0.0) or 0.0)
                    fees_usd = float(last_exit.get("fees_total", 0.0) or 0.0)
                    equity += (pnl_usd - fees_usd)
                    last_flat_i = i

                open_position = False
                current_bracket = None
                open_side = None
                open_qty = 0.0
                open_entry_px = None
                open_entry_ts = None
                open_be_locked = False
                continue

        # guarded BE lock
        if open_position and current_bracket and open_entry_ts is not None and not open_be_locked:
            if _minutes_between(open_entry_ts, bar["t_local"]) >= BE_LOCK_MIN:
                tick = float(fees["tick_size"])
                entry_px = float(open_entry_px)
                px_now = float(bar["close"])
                moved = (
                    (open_side == "BUY" and px_now >= entry_px + BE_REQ_TICKS * tick) or
                    (open_side == "SELL" and px_now <= entry_px - BE_REQ_TICKS * tick)
                )
                if moved:
                    if open_side == "BUY":
                        current_bracket.stop_price = max(float(current_bracket.stop_price), entry_px)
                    else:
                        current_bracket.stop_price = min(float(current_bracket.stop_price), entry_px)
                    open_be_locked = True

        # time-based exit
        if open_position and current_bracket and open_entry_ts is not None:
            if _minutes_between(open_entry_ts, bar["t_local"]) >= MAX_HOLD_MIN:
                before = len(om.trades)
                exit_info = om.simulate_bracket(bar, current_bracket)
                exited = bool(exit_info) or (len(om.trades) > before and om.trades[-1].get("action") == "EXIT")
                if not exited:
                    exit_px = float(bar["close"])
                    side = (open_side or "BUY")
                    qty = float(open_qty or getattr(current_bracket, "qty", 0) or 0.0)
                    entry_px = float(open_entry_px if open_entry_px is not None else bar["close"])
                    side_mult = 1.0 if side == "BUY" else -1.0
                    if crypto_like or equity_like:
                        pnl = (exit_px - entry_px) * side_mult * qty
                    else:
                        pnl = (exit_px - entry_px) * dpp * side_mult * qty

                    om.trades.append({
                        "ts": bar.get("ts") or bar.get("datetime") or bar.get("t_utc") or None,
                        "t_local": bar["t_local"],
                        "action": "EXIT",
                        "side": side,
                        "qty": float(qty),
                        "price": entry_px,
                        "reason": "TIME",
                        "exit_price": exit_px,
                        "pnl": pnl,
                        "fees_total": 0.0,
                    })

                    try:
                        if open_risk_pts and float(open_risk_pts) > 0:
                            pnl_pts = (exit_px - entry_px) * (1.0 if side == "BUY" else -1.0)
                            R = pnl_pts / float(open_risk_pts)
                            risk.record_trade_outcome_R(R)
                            om.trades[-1]["R"] = float(R)
                    except Exception:
                        pass

                    equity += pnl
                    last_flat_i = i

                open_position = False
                current_bracket = None
                open_side = None
                open_qty = 0.0
                open_entry_px = None
                open_entry_ts = None
                open_be_locked = False
                continue

        # new signal
        if not open_position and not risk.halted:
            if COOLDOWN_BARS > 0 and (i - last_flat_i) < COOLDOWN_BARS:
                why["skip_cooldown"] += 1
                continue

            sig = strat.maybe_signal(bar, windows, risk)
            if sig:
                why["signal"] += 1
            else:
                # Try fallback if primary has no signal
                if fallback is not None:
                    sig = fallback.maybe_signal(bar, windows, risk)
                    if sig:
                        why["signal_fallback"] += 1
                        why[f"signal_fallback_{fallback_name}"] += 1
                    else:
                        why["no_signal"] += 1
                        continue
                else:
                    why["no_signal"] += 1
                    continue
            if not sig:
                why["no_signal"] += 1
                continue

            if _minutes_to_window_end(bar["t_local"], windows) < MIN_WIN_END:
                why["skip_window_end"] += 1
                continue

            tick_size = float(fees["tick_size"])
            entry = float(getattr(sig["order"], "entry", bar["close"]))
            stop = float(sig["bracket"].stop_price)

            if equity < float(args.min_equity):
                why["skip_low_equity"] += 1
                continue
            
            max_notional_pct = float((cfg.get("risk", {}) or {}).get("max_notional_pct", 1.0))
            max_notional_usd = equity * max_notional_pct

            if crypto_like:
                size = _compute_size_crypto(
                    equity=equity,
                    risk_pct=float(args.risk_pct),
                    entry_px=entry,
                    stop_px=stop,
                    trade_min_qty=trade_min_qty,
                    max_notional_usd=max_notional_usd,
                    max_qty=None,  # or wire a yaml knob like max_qty if you want
                )
            else:
                stop_ticks = abs(entry - stop) / tick_size if tick_size else 0.0
                size = _compute_size(
                    symbol=symbol,
                    equity=equity,
                    risk_pct=float(args.risk_pct),
                    stop_ticks=stop_ticks,
                    tick_value=float(fees["tick_value"]),
                    last_price=float(bar["close"]),
                    max_size=int(args.max_size),
                    equity_like=equity_like,
                    entry_px=entry,
                    stop_px=stop,
                    crypto_like=crypto_like,
                    trade_min_qty=trade_min_qty,
                )

            min_notional_usd = float((cfg.get("risk", {}) or {}).get("min_notional_usd", 0.0) or 0.0)
            if min_notional_usd > 0:
                notional = float(entry) * float(size)
                if notional < min_notional_usd:
                    why["skip_min_notional"] += 1
                    continue


            if size < 1e-12:
                why["skip_size_lt_min"] += 1
                continue

            why["entry_submitted"] += 1

            if crypto_like:
                sig["order"].qty = float(size)
                if hasattr(sig["bracket"], "qty"):
                    sig["bracket"].qty = float(size)
            else:
                sig["order"].qty = int(size)
                if hasattr(sig["bracket"], "qty"):
                    sig["bracket"].qty = int(size)
            risk_pts = max(float(fees["tick_size"]), abs(entry - stop))
            if delay.bars > 0:
                delay.submit(i, {
                    "order": sig["order"], 
                    "bracket": sig["bracket"], 
                    "entry": entry,
                    "risk_pts": risk_pts,
                })
            else:
                om.place_and_simulate(bar, sig["order"])
                current_bracket = sig["bracket"]
                open_position = True
                open_side = str(sig["order"].side).upper()
                open_qty = float(sig["order"].qty)
                open_entry_px = entry
                open_entry_ts = bar["t_local"]
                open_risk_pts = risk_pts
        else:
            if risk.halted:
                why["risk_halted"] += 1
                if why["risk_halted"] == 1:
                    print(f"[RISK HALT] {symbol} reason={getattr(risk, 'halt_reason', None)} snapshot={risk.snapshot()}")
                if not halted_prev:
                    # print once when halt first triggers for the day/session
                    halted_prev = True
                    print(
                        f"[RISK HALT] {symbol} date={bar['date']} t_local={bar['t_local']} "
                        f"equity={equity:.2f} realized_pnl={getattr(om, 'realized_pnl', 0.0)}"
                    )
            else:
                halted_prev = False

    # ---- Results ----
    trades_df = pd.DataFrame(om.trades)
    if trades_df.empty:
        trades_df = pd.DataFrame(columns=[
            "ts","t_local","action","side","qty","entry_price","price",
            "reason","exit_price","pnl","fees_total","R"
        ])

    exits = trades_df.loc[trades_df["action"] == "EXIT"] if ("action" in trades_df.columns) else pd.DataFrame()
    trades_n = int(len(exits)) if exits is not None else 0

    total_pnl = float(exits["pnl"].sum()) if (exits is not None and not exits.empty and "pnl" in exits.columns) else 0.0
    winrate = float((exits["pnl"] > 0).mean() * 100.0) if (exits is not None and not exits.empty and "pnl" in exits.columns) else 0.0

    # Artifacts per symbol
    safe_sym = symbol.replace("/", "_")
    logs_dir = Path("logs") / f"bt_{safe_sym}"
    logs_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(logs_dir / "backtest_trades.csv", index=False)

    pnl_series = equity_curve(om.trades)
    if args.no_gui:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if pnl_series is not None and not pnl_series.empty:
        ax = pnl_series.plot(title=f"{symbol} PnL (USD)")
        fig = ax.get_figure()
        fig.savefig(logs_dir / "pnl_curve.png", dpi=120, bbox_inches="tight")
        if not args.no_gui:
            plt.show()
        plt.close(fig)

    # Build a minimal equity series for summarize_equity() / summary.json
    equity_full = pd.Series(index=df.index, dtype=float)
    if len(equity_full.index) > 0:
        equity_full.iloc[0] = float(args.starting_equity)
        equity_full = equity_full.ffill().fillna(float(args.starting_equity))
        equity_full.iloc[-1] = float(equity)
    else:
        equity_full = pd.Series([float(equity)])

    summary = summarize_equity(equity_full, trades_df)

    # ---- WHY diagnostics (always print top reasons) ----
    top = sorted(why.items(), key=lambda kv: kv[1], reverse=True)[:12]
    print(f"[WHY] {symbol} top reasons:", top)

    # Optional: still call out zero/low trade runs explicitly
    trades_ct = int(summary.get("Trades", 0) or 0)
    if trades_ct == 0:
        print(f"[WHY] {symbol} ZERO TRADES — check filters/size gates")
    elif trades_ct <= 3:
        print(f"[WHY] {symbol} LOW TRADES ({trades_ct}) — likely over-filtered")


    summary["EndEquity"] = float(equity)
    write_artifacts(equity_full, trades_df, summary, logs_dir=str(logs_dir))
    if int(summary.get("Trades", 0) or 0) == 0:
        print(f"[WHY] {symbol} top reasons: {why.most_common(8)}")

    print(f"[RESULT] {symbol} Trades={summary.get('Trades', 0)} WinRate={summary.get('WinRate', 0):.1f}% "
          f"PF={summary.get('ProfitFactor')} NetPnL=${summary.get('NetPnL', 0):.2f} logs={logs_dir}")

    return {
        "Symbol": symbol,
        "CSV": csv_path,
        "Trades": int(summary.get("Trades", 0) or 0),
        "WinRate": float(summary.get("WinRate", 0.0) or 0.0),
        "ProfitFactor": summary.get("ProfitFactor"),
        "NetPnL": float(summary.get("NetPnL", 0.0) or 0.0),
        "Status": "OK",
        "Why": dict(why),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts", default="config/contracts.yaml")
    ap.add_argument("--profile", default="config/profile/futures.yaml")
    ap.add_argument("--env", default=None, help="Optional env yaml to merge")
    ap.add_argument("--config", default=None, help="Optional extra yaml to merge last")

    # NEW: universe support
    ap.add_argument("--universe", default=None, help="Universe yaml (config/universes/<name>.yaml)")
    ap.add_argument("--all", action="store_true", help="Run all symbols in universe")
    ap.add_argument("--csv-dir", default=None, help="Directory holding per-symbol CSVs (data/)")
    ap.add_argument("--portfolio", default=None, help="Portfolio yaml (config/portfolios/mixed_1k.yaml)")
    ap.add_argument("--microlot", default=None, help="Microlot name inside portfolio (e.g., EQ-SWI)")

    ap.add_argument("--symbol", default=None, help="Symbol (if not --all)")
    ap.add_argument("--csv", dest="csv", default=None, help="CSV path for backtest (overrides --csv-dir pattern)")
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--fee-fixed", type=float, default=0.0)
    ap.add_argument("--slip-bps", type=float, default=0.5)
    ap.add_argument("--latency-bars", dest="latency_bars", type=int, default=0)
    ap.add_argument("--starting-equity", type=float, default=25000.0)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    ap.add_argument("--max-size", type=int, default=3)
    ap.add_argument("--min-equity", type=float, default=0.0)
    ap.add_argument("--start-date", dest="start_date", default=None, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end-date", dest="end_date", default=None, help="YYYY-MM-DD inclusive")



    args = ap.parse_args()

    portfolio_cfg = _load_yaml_if_exists(args.portfolio)

    # If user provided portfolio+microlot, apply defaults from it
    if args.portfolio and args.microlot:
        ml = _pick_microlot(portfolio_cfg, args.microlot)

        # universe
        if not args.universe:
            args.universe = _resolve_universe_path(ml["universe"])

        # sizing defaults
        if args.starting_equity == 25000.0:  # only override if user didn't intentionally set it
            args.starting_equity = float(ml.get("cash_alloc", args.starting_equity))

        if args.risk_pct == 0.01:
            args.risk_pct = float(ml.get("max_risk_per_trade", args.risk_pct))

        if args.max_size == 3:
            # OPTIONAL: treat max_pos as max-size cap for backtest (safety rail)
            args.max_size = int(ml.get("max_pos", args.max_size))



    universe_cfg = _load_yaml_if_exists(args.universe)

    # Decide symbols
    symbols = []
    if args.all:
        symbols = [s.upper() for s in (universe_cfg.get("symbols") or [])]
        if not symbols:
            raise SystemExit("You passed --all but universe has no symbols.")
    else:
        if not args.symbol:
            raise SystemExit("Pass --symbol <SYM> or use --all with --universe.")
        symbols = [args.symbol.upper()]

    results = []
    for sym in symbols:
        # Choose CSV path
        if args.csv:
            csv_path = args.csv
        else:
            base = args.csv_dir or "data"
            csv_sym = _symbol_to_csv_stem(sym)
            csv_path = str(Path(base) / f"{csv_sym}_live_1m.csv")

        r = run_one_symbol(args=args, symbol=sym, csv_path=csv_path, universe_cfg=universe_cfg if args.universe else None)
        results.append(r)

    # Print combined summary
    df = pd.DataFrame(results)
    print("\n=== BACKTEST SUMMARY ===")
    cols = [c for c in ["Symbol", "Status", "Trades", "WinRate", "ProfitFactor", "NetPnL", "CSV"] if c in df.columns]
    print(df[cols].to_string(index=False))

    ok = df[df["Status"] == "OK"] if "Status" in df.columns else df
    if not ok.empty and "NetPnL" in ok.columns:
        print(f"\n[COMBINED] Symbols OK={len(ok)} TotalNetPnL=${ok['NetPnL'].sum():.2f}")


if __name__ == "__main__":
    main()
