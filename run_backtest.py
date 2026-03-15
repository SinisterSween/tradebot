# run_backtest.py
import argparse
import os
import sys
from pathlib import Path
from collections import Counter
from datetime import date
from matplotlib.pyplot import bar
from typing import Optional
import yaml
import pandas as pd
import matplotlib
import numpy as np
import json, hashlib


from trader.engine.backtest import prepare_bars, equity_curve
from trader.utils.run_manifest import write_run_manifest
from trader.engine.execution import OrderManager
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig as OrbCfg
from trader.strategies.rsi_mean_reversion import RsiMeanReversion, StratConfig as RsiMRConfig
from trader.strategies.vwap_reversion import VwapReversion, StratConfig as RevCfg
from trader.strategies.trend_pullback import TrendPullback, StratConfig as PbCfg
from trader.engine.latency import BarDelay
from trader.engine.metrics_ext import summarize_equity, write_artifacts
from dataclasses import fields, is_dataclass

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

def _reanchor_bracket_to_fill(side: str, sig_entry: float, sig_stop: float, sig_target: float, fill_entry: float):
    side = (side or "").upper().strip()
    stop_dist = abs(float(sig_entry) - float(sig_stop))
    tgt_dist  = abs(float(sig_target) - float(sig_entry))

    if side == "BUY":
        stop_px   = float(fill_entry) - stop_dist
        target_px = float(fill_entry) + tgt_dist
    else:  # SELL
        stop_px   = float(fill_entry) + stop_dist
        target_px = float(fill_entry) - tgt_dist

    return float(stop_px), float(target_px)

def _round_to_tick(px: float, tick_size: float) -> float:
    t = float(tick_size or 0.0)
    if t <= 0:
        return float(px)
    return round(float(px) / t) * t


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

def _as_bool(v, default=False) -> bool:
    if v is None:
        return bool(default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    if s in {"false", "0", "no", "n", "off"}:
        return False
    return bool(default)

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

def _maybe_move_stop_to_be(
    bar,
    *,
    open_side: str,
    open_entry_px: float,
    open_entry_ts: str,          # "HH:MM"
    current_bracket,             # bracket object with .stop_price
    tick_size: float,
    be_req_ticks: int,
    be_lock_min: int,
    be_armed: bool,
):
    """
    Returns (current_bracket, new_be_armed, reason_or_None)

    - Uses bar["t_local"] and open_entry_ts (both "HH:MM") with _minutes_between()
    - Mutates current_bracket.stop_price in-place (what simulate_bracket expects)
    """
    if be_armed:
        return current_bracket, True, None

    if current_bracket is None or open_entry_ts is None:
        return current_bracket, be_armed, None

    # lockout window (minutes since entry)
    try:
        mins_open = _minutes_between(open_entry_ts, bar["t_local"])
    except Exception:
        return current_bracket, be_armed, None

    if mins_open < float(be_lock_min):
        return current_bracket, be_armed, None

    px = float(bar["close"])
    req_move = float(be_req_ticks) * float(tick_size)

    side = (open_side or "").upper()
    stop_now = float(getattr(current_bracket, "stop_price", float("nan")))

    if side == "BUY":
        moved_enough = (px - float(open_entry_px)) >= req_move
        be_stop = float(open_entry_px)  # BE; change to +tick_size for BE+1 tick if desired
        tighten = moved_enough and (stop_now < be_stop)
        if tighten:
            current_bracket.stop_price = max(float(current_bracket.stop_price), be_stop)
            return current_bracket, True, "be_moved"

    elif side == "SELL":
        moved_enough = (float(open_entry_px) - px) >= req_move
        be_stop = float(open_entry_px)
        tighten = moved_enough and (stop_now > be_stop)
        if tighten:
            current_bracket.stop_price = min(float(current_bracket.stop_price), be_stop)
            return current_bracket, True, "be_moved"

    return current_bracket, be_armed, None


def _norm_strat_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "HybridOrbVwap"

    key = s.replace("-", "_").replace(" ", "_").lower()

    aliases = {
        # ORB / breakout variants
        "hybrid_orb_vwap":    "HybridOrbVwap",
        "hybridorbvwap":      "HybridOrbVwap",
        "orb_vwap":           "HybridOrbVwap",
        "orb":                "HybridOrbVwap",
        "tinycapbreakout":    "HybridOrbVwap",   # tiny_cap.yaml uses ORB params
        "leveredetfbreakout": "HybridOrbVwap",   # levered_etf.yaml uses ORB params

        # VWAP reversion
        "vwap_reversion":     "VwapReversion",
        "vwapreversion":      "VwapReversion",
        "vwap":               "VwapReversion",

        # Trend pullback
        "trend_pullback":     "TrendPullback",
        "trendpullback":      "TrendPullback",

        # RSI mean reversion
        "rsi_mean_reversion": "RsiMeanReversion",
        "rsimeanreversion":   "RsiMeanReversion",
        "rsi":                "RsiMeanReversion",
    }

    return aliases.get(key, s)  # if already "VwapReversion", keep it

_STRATEGY_META_KEYS = {
    # common meta keys used by runner / prepare_bars
    "name", "session_windows",
    "orb_minutes",
    "ema_fast_len", "ema_slope_lookback",
    "rsi_len", "atr_len",
    # keep these if you ever put them at strategy level
    "timeframe", "bar_size", "backfill",
    "fallback"
}

def warn_unused_keys(cfg_cls, s_cfg: dict, context: str = ""):
    if not is_dataclass(cfg_cls):
        return
    allowed = {f.name for f in fields(cfg_cls)} | _STRATEGY_META_KEYS
    extra = sorted(set((s_cfg or {}).keys()) - allowed)
    if extra:
        msg = f"Unused keys for {cfg_cls.__name__} {context}: {extra}"
        if os.getenv("FAIL_ON_UNUSED_KEYS", "0") == "1":
            raise ValueError(msg)
        print(f"[CFG][WARN] {msg}")

def _filter_kwargs(dataclass_type, kwargs: dict) -> dict:
    """Return only kwargs that exist on a dataclass."""
    if not is_dataclass(dataclass_type):
        return kwargs
    allowed = {f.name for f in fields(dataclass_type)}
    return {k: v for k, v in kwargs.items() if k in allowed}

def deep_merge(a: dict, b: dict) -> dict:
    """Return a deep-merged copy of a <- b (b wins)."""
    out = dict(a or {})
    for k, v in (b or {}).items():
        # --- SPECIAL CASE: strategy blocks ---
        # If both sides have strategy dicts and strategy name changes,
        # DO NOT deep-merge; replace entirely to avoid key leakage.
        if k == "strategy" and isinstance(v, dict) and isinstance(out.get(k), dict):
            a_name = (out[k].get("name") or "").strip()
            b_name = (v.get("name") or "").strip()
            if a_name and b_name and a_name.lower() != b_name.lower():
                out[k] = dict(v)  # replace strategy block
                continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def merge_strategy_cfg(base: dict | None, ovr: dict | None) -> dict:
    base = dict(base or {})
    ovr  = dict(ovr or {})

    a_name = (base.get("name") or "").strip()
    b_name = (ovr.get("name") or "").strip()

    # If strategy name changes, replace entirely (prevents key leakage like target_vwap)
    if a_name and b_name and a_name.lower() != b_name.lower():
        return ovr

    # Otherwise normal deep merge (ovr wins)
    return deep_merge(base, ovr)

REQUIRED_BY_STRATEGY = {
    "rsi_mean_reversion": {
        "rsi_len",
        "rsi_buy_below",
        "rsi_sell_above",
        "atr_len",
        "atr_mult",
        "target_R",
        "stop_pad_ticks",
    },
    "vwap_reversion": {
        "atr_len",
        "atr_mult",
        "target_R",
        "stop_pad_ticks",
    },
    "hybrid_orb_vwap": {
        "orb_minutes",
        "atr_len",
        "atr_mult",
        "target_R",
    },
}

def assert_locked_strategy_complete(
    cfg: dict,
    *,
    symbol: str | None = None, 
    strategy_name: str | None = None,
    strategy_cfg: dict | None = None,
):

    if not cfg.get("locked", False):
        return
    
    sym = symbol or cfg.get("symbol") or "<unknown>"

    strat = strategy_cfg if strategy_cfg is not None else cfg.get("strategy") or {}
    if not isinstance(strat, dict):
        raise ValueError(f"[LOCKED CONFIG ERROR] {sym} strategy must be a dict")
    
    if strategy_name is None:
        raw = strat.get("name") or cfg.get("strategy_name") or "HybridOrbVwap"
        strategy_name = _norm_strat_name(raw)

    required = REQUIRED_BY_STRATEGY.get(strategy_name)
    if not required:
        return  # no rule for this strategy name

    missing = required - set(strat.keys())
    if missing:
        raise ValueError(
            f"[LOCKED CONFIG ERROR] {sym} ({strategy_name}) missing keys: {sorted(missing)}"
        )


def build_strategy(strategy_name: str, s_cfg: dict, fees: dict):
    name = _norm_strat_name(strategy_name or "HybridOrbVwap")
    debug_echo = (os.getenv("STRAT_ECHO", "0") == "1")
    if name == "VwapReversion":
        warn_unused_keys(RevCfg, s_cfg, context=f"({name})")
        rev_kwargs = dict(
            atr_len=int(s_cfg.get("atr_len", 14)),
            atr_mult=float(s_cfg.get("atr_mult", 1.0)),
            target_R=float(s_cfg.get("target_R", 1.0)),
            slope_max=float(s_cfg.get("slope_max", 0.03)),
            stop_pad_ticks=int(s_cfg.get("stop_pad_ticks", 2)),
            tick_size=float(fees["tick_size"]),
            tick_value=float(fees["tick_value"]),
            target_vwap=_as_bool(s_cfg.get("target_vwap", True)),
            min_atr_pct=float(s_cfg.get("min_atr_pct", 0.0)),
            max_bar_range_atr=float(s_cfg.get("max_bar_range_atr", 0.0)),
            ema_slope_max=float(s_cfg.get("ema_slope_max", 0.0)),
            ema_max_dist_atr=float(s_cfg.get("ema_max_dist_atr", 0.0)),
            vwap_entry_min_atr=float(s_cfg.get("vwap_entry_min_atr", 0.0)),
            vwap_entry_max_atr=float(s_cfg.get("vwap_entry_max_atr", 0.0)),
            min_vwap_target_R=float(s_cfg.get("min_vwap_target_R", 0.0)),
            orb_minutes=int(s_cfg.get("orb_minutes", 0)),
            allow_longs=_as_bool(s_cfg.get("allow_longs", True)),
            allow_shorts=_as_bool(s_cfg.get("allow_shorts", True)),
            ema_slope_buy_min=float(s_cfg.get("ema_slope_buy_min", -999.0)),
            ema_slope_sell_max=float(s_cfg.get("ema_slope_sell_max", 999.0)),
        )
        cfg = RevCfg(**_filter_kwargs(RevCfg, rev_kwargs))
        return VwapReversion(cfg, debug_cfg_echo=debug_echo)

    if name == "TrendPullback":
        warn_unused_keys(PbCfg, s_cfg, context=f"({name})")
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
            min_notional_usd=float(s_cfg.get("min_notional_usd", 0.0)),
        )
        cfg = PbCfg(**_filter_kwargs(PbCfg, pb_kwargs))
        return TrendPullback(cfg, debug_cfg_echo=debug_echo)
    
    if name == "RsiMeanReversion":
        warn_unused_keys(RsiMRConfig, s_cfg, context=f"({name})")
        pb_kwargs = dict(
            rsi_len=int(s_cfg.get("rsi_len", 14)),
            rsi_buy_below=float(s_cfg.get("rsi_buy_below", 28)),
            rsi_sell_above=float(s_cfg.get("rsi_sell_above", 72)),
            atr_mult=float(s_cfg.get("atr_mult", 1.0)),
            target_R=float(s_cfg.get("target_R", s_cfg.get("take_profit_R", 0.8))),
            stop_pad_ticks=int(s_cfg.get("stop_pad_ticks", 2)),
            slope_max=float(s_cfg.get("slope_max", 0.0)),
            min_atr_pct=float(s_cfg.get("min_atr_pct", 0.0)),
            target_vwap=_as_bool(s_cfg.get("target_vwap", True)),
            require_cross=_as_bool(s_cfg.get("require_cross", True)),
            min_minutes_between_trades=int(s_cfg.get("min_minutes_between_trades", 30)),
            max_trades_per_day=int(s_cfg.get("max_trades_per_day", 0)),
            ema_slope_max=float(s_cfg.get("ema_slope_max", 0.0)),
            ema_dist_atr_min=float(s_cfg.get("ema_dist_atr_min", 0.0)),
            ema_dist_atr_max=float(s_cfg.get("ema_dist_atr_max", 0.0)),
            ema_slope_atr_min=float(s_cfg.get("ema_slope_atr_min", -0.2)),
            exit_on_ema=bool(s_cfg.get("exit_on_ema", False)),
            exit_ema_len=int(s_cfg.get("exit_ema_len", 20)),
            exit_ema_side=str(s_cfg.get("exit_ema_side", "cross")),
            allow_longs=_as_bool(s_cfg.get("allow_longs", True)),
            allow_shorts=_as_bool(s_cfg.get("allow_shorts", False)),
            tick_size=float(fees.get("tick_size", 0.01)),
            tick_value=float(fees.get("tick_value", 1.0)),
            vwap_entry_min_atr=float(s_cfg.get("vwap_entry_min_atr", 0.0)),
            vwap_entry_max_atr=float(s_cfg.get("vwap_entry_max_atr", 0.0)),
            vwap_dist_atr_min=float(s_cfg.get("vwap_dist_atr_min", 0.0)),
            min_vwap_target_R=float(s_cfg.get("min_vwap_target_R", 0.0)),
            use_session_open_stop=_as_bool(s_cfg.get("use_session_open_stop", False)),
            session_open_stop_atr_pad=float(s_cfg.get("session_open_stop_atr_pad", 0.0)),
            use_early_range_target=_as_bool(s_cfg.get("use_early_range_target", False)),
            early_range_mult=float(s_cfg.get("early_range_mult", 1.0)),
            early_min_range_atr=float(s_cfg.get("early_min_range_atr", 0.0)),
            require_early_done=_as_bool(s_cfg.get("require_early_done", True)),
        )
        cfg = RsiMRConfig(**_filter_kwargs(RsiMRConfig, pb_kwargs))
        return RsiMeanReversion(cfg, debug_cfg_echo=debug_echo)


    # default: HybridOrbVwap
    warn_unused_keys(OrbCfg, s_cfg, context=f"({name})")
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
    return HybridOrbVwap(cfg, debug_cfg_echo=debug_echo)

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
    if risk_per_unit <= 0 or entry_px <= 0:
        return 0.0

    qty_by_risk = risk_budget / risk_per_unit

    if max_notional_usd is None:
        max_notional_usd = float(equity)

    # affordability in notional terms (still matters)
    qty_by_notional = float(max_notional_usd) / float(entry_px)

    qty = min(qty_by_risk, qty_by_notional)

    if max_qty is not None:
        qty = min(qty, float(max_qty))
    
    if trade_min_qty and trade_min_qty > 0:
        qty = _floor_to_step(qty, float(trade_min_qty))
        if qty < float(trade_min_qty):
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

def _norm_sym(s: str | None) -> str:
    if not s:
        return ""
    return str(s).strip().upper().replace(" ", "")

def _find_symbol_row(universe_cfg: dict | None, symbol: str) -> Optional[dict]:
    """
    universe_cfg format:
      symbols:
        - symbol: XRP/USDT
          strategy: ...
          execution: ...
    """
    if not universe_cfg:
        return None
    rows = universe_cfg.get("symbols") or []
    if not isinstance(rows, list):
        return None
    target = _norm_sym(symbol)
    matches = [
        row for row in rows
        if isinstance(row, dict) and _norm_sym(row.get("symbol")) == target
    ]

    if len(matches) > 1:
        print(f"[WARN] duplicate symbol rows for {symbol}: {len(matches)} (using first)")

    return matches[0] if matches else None

def _load_yaml_if_exists(path: str | None) -> dict:
    if not path:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _resolve_contract_for_symbol(symbol: str, contracts_path: str, universe: dict | None) -> dict:
    contracts = _load_yaml_if_exists(contracts_path)
    sym_u = symbol.strip().upper()

    # Prefer explicit contract definition
    if sym_u in contracts:
        return dict(contracts[sym_u] or {})

    # Otherwise build an equity-like contract from universe defaults
    uni = universe or {}
    tick_size = float(uni.get("tick_size", 0.01))
    point_value = float(uni.get("point_value", 1.0))

    asset_class = str(uni.get("asset_class", "")).lower()
    exchange = str(uni.get("exchange", "")).lower()

    is_crypto = (asset_class == "crypto") or ("/" in sym_u) or (exchange in {"binanceus", "kraken", "coinbase"})
    if is_crypto:
        
        return {
            "symbol": sym_u,
            "exchange": exchange.upper() if exchange else "CRYPTO",
            "currency": "USD",
            "tick_size": tick_size,
            "tick_value": tick_size,
            "commission_per_contract": 0.0,
            "exchange_fees_per_contract": 0.0,
            "_point_value": point_value,
        }
    # For equities we want dollars-per-point to be 1 (per share).
    # Our engine uses dpp = tick_value / tick_size, so set tick_value = tick_size * 1 => dpp=1.
    return {
        "symbol": sym_u,
        "exchange": "SMART",
        "currency": "USD",
        "tick_size": tick_size,
        "tick_value": float(tick_size) * 1.0,
        # Use a sane default if not given elsewhere
        "commission_per_contract": 0.0,
        "exchange_fees_per_contract": 0.0,
        # keep universe point_value around as an extra field if you want later
        "_point_value": point_value,
    }
def _mark_entry_for_throttles(strat, bar):
    # only count throttles when we actually submit an entry
    if hasattr(strat, "_trades_today"):
        strat._trades_today += 1
    if hasattr(strat, "_last_trade_ts"):
        strat._last_trade_ts = bar.name



def run_one_symbol(*, args, symbol: str, csv_path: str, universe_cfg: dict | None) -> dict:
    # ---- Load layered config: profile -> env -> config(extra) ----
    cfg = {}
    cfg = deep_merge(cfg, _load_yaml_if_exists(args.profile))
    cfg = deep_merge(cfg, _load_yaml_if_exists(args.env))
    cfg = deep_merge(cfg, _load_yaml_if_exists(args.config))

    # Stash profile name for equity heuristic
    cfg["profile_name"] = os.path.basename(args.profile or "")

    # Merge universe strategy over profile strategy (universe is closer to “intent”)
    if universe_cfg:

        uc = dict(universe_cfg)
        uc.pop("symbols", None)  # don't let symbols pollute cfg

        cfg = deep_merge(cfg, uc)

        if "strategy" in universe_cfg:
            cfg.setdefault("strategy", {})
            cfg["strategy"] = merge_strategy_cfg(cfg.get("strategy", {}), universe_cfg.get("strategy", {}))
        
        # Execution merge (global)
        if "execution" in universe_cfg:
            cfg.setdefault("execution", {})
            cfg["execution"] = deep_merge(cfg.get("execution", {}), universe_cfg.get("execution", {}))

        # Risk merge (global) — important for crypto_all.yaml
        if "risk" in universe_cfg:
            cfg.setdefault("risk", {})
            cfg["risk"] = deep_merge(cfg.get("risk", {}), universe_cfg.get("risk", {}))

    # --- Capture universe/base strategy as fallback BEFORE per-symbol overrides ---
    import copy
    base_strategy_cfg = copy.deepcopy(cfg.get("strategy") or {})
    # base_strategy_cfg = dict((cfg.get("strategy") or {}))
    base_strategy_name = (base_strategy_cfg.get("name") or "").strip()


    # ✅ Per-symbol overrides (NEW)
    sym_row = _find_symbol_row(universe_cfg, symbol)
    if sym_row:
        # Merge any top-level symbol-specific fields (tick_size, trade_min_qty, rotate, etc.)
        sym_top = dict(sym_row)
        sym_top.pop("symbol", None)
        sym_top.pop("strategy", None)
        sym_top.pop("execution", None)
        sym_top.pop("risk", None)
        cfg = deep_merge(cfg, sym_top)

        # strategy override
        if "strategy" in sym_row:
            cfg.setdefault("strategy", {})
            cfg["strategy"] = merge_strategy_cfg(cfg.get("strategy", {}), sym_row.get("strategy", {}))

        # execution override
        if "execution" in sym_row:
            cfg.setdefault("execution", {})
            cfg["execution"] = deep_merge(cfg.get("execution", {}), sym_row.get("execution", {}))

        # risk override
        if "risk" in sym_row:
            cfg.setdefault("risk", {})
            cfg["risk"] = deep_merge(cfg.get("risk", {}), sym_row.get("risk", {}))


    # ---- Contract / fees ----
    ct = _resolve_contract_for_symbol(symbol, args.contracts, universe_cfg)
    cfg.setdefault("fees", {})
    fees = cfg["fees"]

    asset_class = str((universe_cfg or {}).get("asset_class", "")).lower()

    if "tick_size" in cfg:
        cfg["fees"]["tick_size"] = float(cfg["tick_size"])

    for k in ("commission_per_contract", "exchange_fees_per_contract"):
        if k in ct:
            fees[k] = ct[k]
    # 2) Tick sizing rules:
    if asset_class == "crypto":
        # Prefer YAML tick_size if present (because you override per symbol there)
        if "tick_size" not in fees and "tick_size" in ct:
            fees["tick_size"] = ct["tick_size"]

        # Enforce dpp=1: tick_value MUST equal tick_size
        if "tick_size" in fees:
            fees["tick_value"] = float(fees["tick_size"])

    else:
        # Futures/equities: ct defines tick_size + tick_value (if present)
        for k in ("tick_size", "tick_value"):
            if k in ct:
                fees[k] = ct[k]

    cfg["symbol"] = ct.get("symbol", symbol)

    # --- Invariant guard: dollars-per-point (dpp) must be sane ---
    tick_size  = float(cfg["fees"]["tick_size"])
    tick_value = float(cfg["fees"]["tick_value"])
    fees["dpp"] = float(fees["tick_value"]) / float(fees["tick_size"])
    dpp = float(fees["dpp"])

    asset_class = str((universe_cfg or {}).get("asset_class", "")).lower()
    if asset_class == "crypto" and abs(dpp - 1.0) > 1e-6:
        raise ValueError(
            f"[DPP] crypto must be dpp=1. got {dpp}. "
            f"tick_value={tick_value} tick_size={tick_size} symbol={symbol}"
        )

    def _fingerprint(obj) -> str:
        s = json.dumps(obj, sort_keys=True, default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    # Pick only the stuff that should define behavior
    finger_obj = {
        "symbol": symbol,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "strategy": cfg.get("strategy"),
        "execution": cfg.get("execution"),
        "risk": cfg.get("risk"),
        "fees": cfg.get("fees"),
        "timezone": cfg.get("timezone"),
    }

    if not csv_path or not os.path.exists(csv_path):
        return {
            "Symbol": symbol,
            "CSV": csv_path,
            "Status": "MISSING_CSV",
        }
    if args.strict:
        if not args.start_date or not args.end_date:
            raise SystemExit("[STRICT] Must pass --start-date and --end-date")
        if not (cfg.get("strategy") or {}).get("name"):
            raise SystemExit("[STRICT] strategy.name missing in merged config")
    # ---- Prep bars ----
    tz = cfg.get("timezone", "America/Chicago")
    df = pd.read_csv(csv_path)
    orb_minutes = int(cfg["strategy"].get("orb_minutes", 5))
    rsi_len = int(cfg["strategy"].get("rsi_len", 14))
    atr_len = int(cfg["strategy"].get("atr_len", 14))

    df = prepare_bars(
        df,
        tz=tz,
        orb_minutes=orb_minutes,
        ema_fast_len=int(cfg["strategy"].get("ema_fast_len", 50)),
        ema_slope_lookback=int(cfg["strategy"].get("ema_slope_lookback", 5)),
        rsi_len=rsi_len,
        atr_len=atr_len,
        symbol=symbol,
        exit_on_ema=bool(cfg["strategy"].get("exit_on_ema", False)),
        exit_ema_len=int(cfg["strategy"].get("exit_ema_len", 20)),
    )


    #df = prepare_bars(df, tz=tz, orb_minutes=orb_minutes)

    # ---- Date filter (keep df['date'] as datetime.date or Timestamp->date) ----
    if args.start_date:
        start_d = pd.to_datetime(args.start_date).date()
        df = df[df["date"] >= start_d]
    if args.end_date:
        end_d = pd.to_datetime(args.end_date).date()
        df = df[df["date"] <= end_d]

    # ensure bars are time-ordered after date filtering
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    # hard-fail if still not monotonic
    if not df.index.is_monotonic_increasing:
        raise ValueError(f"[DATA] {symbol} bars not monotonic after sort")

    # ---- Logs dir (create EARLY so manifest/artifacts are consistent) ----
    safe_sym = symbol.replace("/", "_")
    logs_dir = Path("logs") / f"bt_{safe_sym}"

    if getattr(args, "outdir", None):
        logs_dir = Path(args.outdir)

    if getattr(args, "run_tag", None):
        logs_dir = logs_dir / str(args.run_tag)
    else:
        logs_dir = logs_dir / f"run_{args.run_id}"

    logs_dir.mkdir(parents=True, exist_ok=True)


    # ---- Risk / fees ----
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

    exit_on_ema   = bool(sc.get("exit_on_ema", False))
    exit_ema_len  = int(sc.get("exit_ema_len", 20))
    exit_ema_side = str(sc.get("exit_ema_side", "cross")).strip().lower()  # "cross" or "touch"

    print(f"[EMA CFG] {symbol} exit_on_ema={exit_on_ema} exit_ema_len={exit_ema_len} exit_ema_side={exit_ema_side}")


    windows = _norm_windows(sc.get("session_windows", []))
    if not windows:
        windows = [{"start": "00:00", "end": "23:59"}]

    # ---- SANITY (risk exists now, so this won’t crash) ----
    tradable = df["t_local"].apply(lambda t: _in_windows(t, windows))
    print(
        f"[SANITY] {symbol} bars={len(df)} tradable={int(tradable.sum())} tradable_pct={float(tradable.mean()) if len(df) else 0.0}"
    )

    # ---- Order manager / execution ----
    dpp = float(fees["dpp"])  # dollars per full point
    exe = cfg.get("execution", {}) or {}
    

    commission = float(fees.get("commission_per_contract", 0.0))
    exch_fees   = float(fees.get("exchange_fees_per_contract", 0.0))
    fee_bps     = float(fees.get("fee_bps", 0.0))
    fee_fixed   = float(fees.get("fee_fixed", 0.0))
    slip_bps    = float(fees.get("slip_bps", 0.0))

    if args.fee_bps is not None:
        fee_bps = float(args.fee_bps)
    if args.fee_fixed is not None:
        fee_fixed = float(args.fee_fixed)
    if args.slip_bps is not None:
        slip_bps = float(args.slip_bps)

    if getattr(args, "no_friction", False):
        commission = 0.0
        exch_fees  = 0.0
        fee_bps    = 0.0
        fee_fixed  = 0.0
        slip_bps   = 0.0



    om = OrderManager(
        commission_per_contract=commission,
        exchange_fees_per_contract=exch_fees,
        tick_size=float(fees["tick_size"]),
        fee_bps=fee_bps,
        fee_fixed=fee_fixed,
        slip_bps=slip_bps,
        dollars_per_point=float(dpp),
    )
    delay = BarDelay(bars=int(args.latency_bars or 0))
    

    # ---- Strategy ----

    raw_name = (
        (cfg.get("strategy") or {}).get("name")
        or cfg.get("strategy_name")
        or "HybridOrbVwap"
    )
    strategy_name = _norm_strat_name(raw_name)

    VALID_STRATS = {"HybridOrbVwap", "VwapReversion", "TrendPullback", "RsiMeanReversion"}
    if strategy_name not in VALID_STRATS:
        raise ValueError(f"[STRATEGY] {symbol}: unknown strategy '{raw_name}' -> '{strategy_name}'. Valid={sorted(VALID_STRATS)}")

    print(f"[STRATEGY] {symbol} -> {strategy_name}")

    assert_locked_strategy_complete(cfg, symbol=symbol, strategy_name=strategy_name, strategy_cfg=cfg.get("strategy"))

    strat = build_strategy(strategy_name, cfg["strategy"], fees)

    # ---- Run manifest (after strategy exists; before sim loop) ----
    strat_cfg_obj = getattr(strat, "cfg", None)

    bars_info = {
        "rows": int(len(df)),
        "first_ts": str(df.index[0]) if len(df) else None,
        "last_ts": str(df.index[-1]) if len(df) else None,
        "date_min": str(df["date"].min()) if "date" in df.columns and len(df) else None,
        "date_max": str(df["date"].max()) if "date" in df.columns and len(df) else None,
    }

    manifest_path = write_run_manifest(
        logs_dir,
        cli_argv=sys.argv,
        parsed_args=vars(args),
        symbol=symbol,
        csv_path=csv_path,
        start_date=getattr(args, "start_date", None),
        end_date=getattr(args, "end_date", None),
        resolved_config=cfg,  # merged dict
        strategy_name=strategy_name,
        strategy_kwargs=strat_cfg_obj.__dict__ if strat_cfg_obj is not None else {},
        bars_info=bars_info,
        repo_root=".",
    )
    print(f"[MANIFEST] {manifest_path}")

    # ---- Fallback strategy (universe/base) ----
    fallback = None
    fallback_name = None

    # Primary strategy name (already resolved)
    primary_name = strategy_name

    # Fallback strategy name from universe/base (captured before overrides)
    fb_key_present = "fallback" in (sc or {})
    fb_val = (sc or {}).get("fallback", None)
    # If fallback is explicitly set to null/None/false/"" => disable fallback
    if fb_key_present and (fb_val is None or fb_val is False or str(fb_val).strip().lower() in {"", "none", "null", "false", "0"}):
        fallback_name = None
    else:
        # If fallback is a real name, use it
        if fb_key_present and fb_val:
            fallback_name = _norm_strat_name(str(fb_val))
        # Otherwise: default fallback (your previous behavior)
        elif base_strategy_name:
            fallback_name = _norm_strat_name(base_strategy_name)

    # Only enable fallback if it's different from primary
    if fallback_name and fallback_name != primary_name:
        assert_locked_strategy_complete(cfg, symbol=symbol, strategy_name=fallback_name, strategy_cfg=base_strategy_cfg)
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
    

    om.crypto_like = crypto_like
    om.equity_like = equity_like

    def _force_exit_current_position(
        bar,
        reason: str,
        i: int,
        exit_px: float | None = None,
    ) -> bool:
        """
        Force an exit at bar close (or provided exit_px) when bracket didn't exit.
        Writes an EXIT trade record, updates equity, records R, and resets open state.
        Returns True if exited, False if there was nothing to exit.
        """
        nonlocal equity, last_flat_i
        nonlocal open_position, current_bracket, open_side, open_qty
        nonlocal open_entry_px, open_entry_ts, open_be_locked, open_risk_pts

        if not open_position or current_bracket is None:
            return False

        px = float(exit_px if exit_px is not None else bar["close"])
        side = (open_side or "BUY")
        qty = float(open_qty or getattr(current_bracket, "qty", 0) or 0.0)
        entry_px = float(open_entry_px if open_entry_px is not None else bar["close"])

        side_mult = 1.0 if side == "BUY" else -1.0

        # USD PnL calc
        if crypto_like or equity_like:
            pnl = (px - entry_px) * side_mult * qty
        else:
            pnl = (px - entry_px) * dpp * side_mult * qty
        ts = bar.get("ts") or bar.get("datetime") or bar.get("t_utc") or bar.get("timestamp") or bar.name
        if ts is None:
            print("[EXIT.TS][WARN] ts is None; bar keys:", list(getattr(bar, "keys", lambda: [])()))

        om.trades.append({
            "ts": ts,
            "t_local": bar["t_local"],
            "action": "EXIT",
            "side": side,
            "qty": float(qty),
            "price": px,
            "entry_price": entry_px,
            "reason": reason,
            "exit_price": px,
            "pnl": float(pnl),
            "fees_total": 0.0,
        })

        om.realized_pnl = float(getattr(om, "realized_pnl", 0.0)) + float(pnl)
        om._flat()  # clear internal position so new-day reset doesn't re-open it
        if crypto_like and abs((float(cfg["fees"]["tick_value"]) / float(cfg["fees"]["tick_size"])) - 1.0) > 1e-6:
            print(f"[WARN] crypto_like but dpp != 1: tick_value={cfg['fees']['tick_value']} tick_size={cfg['fees']['tick_size']}")
        print(f"[PNL.DBG] {symbol} side={side} qty={qty} entry={entry_px} exit={px} pnl={pnl} crypto_like={crypto_like} dpp={locals().get('dpp', None)}")


        # Record R if we can
        try:
            if open_risk_pts and float(open_risk_pts) > 0:
                pnl_pts = (px - entry_px) * (1.0 if side == "BUY" else -1.0)
                R = pnl_pts / float(open_risk_pts)
                risk.record_trade_outcome_R(R)
                om.trades[-1]["R"] = float(R)
        except Exception:
            pass

        
        last_flat_i = i  # uses loop variable i from outer scope

        # reset open state
        open_position = False
        current_bracket = None
        open_side = None
        open_qty = 0.0
        open_entry_px = None
        open_entry_ts = None
        open_be_locked = False
        open_risk_pts = None

        return True


    for i, (_, bar) in enumerate(df.iterrows()):
        # new day
        if prev_date is None or bar["date"] != prev_date:
            equity = float(args.starting_equity) + float(getattr(om, "realized_pnl", 0.0))
            risk.reset_day(equity)
            prev_date = bar["date"]

            if float(getattr(om.pos, "qty", 0.0)) == 0.0:
                open_position = False
                current_bracket = None
                open_side = None
                open_qty = 0.0
                open_entry_px = None
                open_entry_ts = None
                open_be_locked = False
                open_risk_pts = None
            else:
                open_position = True

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
            open_position = (float(getattr(om.pos, "qty", 0.0) or 0.0) != 0.0)
            if hasattr(due, "get") and "strat_used" in due:
                s_used = due["strat_used"]
                if hasattr(s_used, "record_entry"):
                    s_used.record_entry(bar.name)
            open_side = str(getattr(om.pos, "side", order.side)).upper()
            open_qty = float(getattr(om.pos, "qty", order.qty))
            open_entry_px = float(getattr(om.pos, "avg_price", entry))   # ✅ USE FILL
            open_entry_ts = bar["t_local"]
            open_be_locked = False

            if current_bracket is not None:
                sig_entry = float(entry)
                sig_stop  = float(current_bracket.stop_price)
                sig_tgt   = float(current_bracket.target_price)

                stop_px, tgt_px = _reanchor_bracket_to_fill(open_side, sig_entry, sig_stop, sig_tgt, open_entry_px)
                stop_px = _round_to_tick(stop_px, float(fees["tick_size"]))
                tgt_px  = _round_to_tick(tgt_px,  float(fees["tick_size"]))

                current_bracket.entry_price  = float(open_entry_px)
                current_bracket.stop_price   = float(stop_px)
                current_bracket.target_price = float(tgt_px)

            if risk_pts_due is not None:
                open_risk_pts = float(risk_pts_due)
            elif current_bracket is not None:
                open_risk_pts = max(float(fees["tick_size"]), abs(float(open_entry_px) - float(current_bracket.stop_price)))
            else:
                open_risk_pts = float(fees["tick_size"])
                
            

        # outside session => flatten-on-close simulation
        if not _in_windows(bar["t_local"], windows):
            if open_position and current_bracket:
                before = len(om.trades)
                exit_info = om.simulate_bracket(
                    bar, 
                    current_bracket,
                )
                exited = bool(exit_info) or (len(om.trades) > before and om.trades[-1].get("action") == "EXIT")

                if not exited:
                    _force_exit_current_position(bar, reason="WINDOW", i=i, exit_px=float(bar["close"]))
                
                equity = float(args.starting_equity) + float(getattr(om, "realized_pnl", 0.0))


            # reset state (safe even if helper already reset)
            open_position = False
            current_bracket = None
            open_side = None
            open_qty = 0.0
            open_entry_px = None
            open_entry_ts = None
            open_be_locked = False
            open_risk_pts = None
            continue


        # -----------------------
        # manage OPEN position
        # -----------------------
        if open_position and current_bracket:
            before = len(om.trades)

            # Debug prints only (DO NOT gate logic behind debug)
            if why.get("dbg_bracket_once", 0) < 5:
                why["dbg_bracket_once"] += 1
                entry = float(open_entry_px)
                stop = float(current_bracket.stop_price)
                target = float(current_bracket.target_price)
                close_px = float(bar["close"])
                high_px = float(bar.get("high", close_px))
                low_px  = float(bar.get("low", close_px))
                print(
                    f"[BRKT.DBG] {symbol} side={open_side} entry={entry:.2f} stop={stop:.2f} target={target:.2f} "
                    f"barL/H/C={low_px:.2f}/{high_px:.2f}/{close_px:.2f} "
                    f"d_stop={abs(entry-stop):.2f} d_tgt={abs(target-entry):.2f}"
                )
                print(
                    f"[POS.DBG] {symbol} open_qty={open_qty} open_side={open_side} open_entry_px={open_entry_px} "
                    f"om.qty={getattr(om.pos,'qty',None)} om.side={getattr(om.pos,'side',None)} om.avg={getattr(om.pos,'avg_price',None)}"
                )

            # Break-even stop adjustment ALWAYS runs
            current_bracket, open_be_locked, be_reason = _maybe_move_stop_to_be(
                bar,
                open_side=open_side,
                open_entry_px=float(open_entry_px),
                open_entry_ts=open_entry_ts,
                current_bracket=current_bracket,
                tick_size=float(fees["tick_size"]),
                be_req_ticks=int(BE_REQ_TICKS),
                be_lock_min=int(BE_LOCK_MIN),
                be_armed=bool(open_be_locked),
            )

            # Bracket simulation ALWAYS runs
            exit_info = om.simulate_bracket(bar, current_bracket)

            # If simulate_bracket returned None but it still wrote an EXIT row, capture it
            if not exit_info and len(om.trades) > before and om.trades[-1].get("action") == "EXIT":
                last = om.trades[-1]
                exit_info = {
                    "side": last.get("side"),
                    "entry_price": float(last.get("price") or last.get("entry_price") or open_entry_px),
                    "exit_price": float(last.get("exit_price") or bar["close"]),
                    "reason": last.get("reason"),
                }

            if exit_info:
                # finalize + reset state
                equity = float(args.starting_equity) + float(getattr(om, "realized_pnl", 0.0))
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


            # ---- EMA-based exit (forced exit; after bracket sim, before BE/time exits) ----
            if open_position and current_bracket and open_entry_ts is not None and exit_on_ema:
                ema_col = f"ema_exit_{exit_ema_len}"
                if ema_col in df.columns:
                    ema = float(bar[ema_col])
                    side = (open_side or "").upper()
                    close_px = float(bar["close"])
                    high_px = float(bar.get("high", close_px))
                    low_px  = float(bar.get("low", close_px))

                    do_exit = False

                    if exit_ema_side == "touch":
                        if side == "BUY":
                            do_exit = high_px >= ema
                        else:  # SELL
                            do_exit = low_px <= ema

                    else:  # "cross"
                        if i > 0:
                            prev_close = float(df["close"].iloc[i - 1])
                            prev_ema = float(df[ema_col].iloc[i - 1])
                            if side == "BUY":
                                do_exit = (prev_close < prev_ema) and (close_px >= ema)
                            else:
                                do_exit = (prev_close > prev_ema) and (close_px <= ema)
                    if do_exit:
                        if why["ema_exit"] < 5:
                            print(f"[EMA EXIT] {symbol} side={side} t={bar['t_local']} close={close_px:.6f} ema={ema:.6f} mode={exit_ema_side}")
                        why["ema_exit"] += 1
                        _force_exit_current_position(bar, reason=f"EMA_EXIT_{exit_ema_len}_{exit_ema_side}", i=i, exit_px=close_px)
                        equity = float(args.starting_equity) + float(getattr(om, "realized_pnl", 0.0))
                        continue
        # time-based exit
        if open_position and current_bracket and open_entry_ts is not None:
            if _minutes_between(open_entry_ts, bar["t_local"]) >= MAX_HOLD_MIN:
                _force_exit_current_position(bar, reason="TIME", i=i, exit_px=float(bar["close"]))
                equity = float(args.starting_equity) + float(getattr(om, "realized_pnl", 0.0))
                continue

        # new signal
        if not open_position and not risk.halted:
            if COOLDOWN_BARS > 0 and (i - last_flat_i) < COOLDOWN_BARS:
                why["skip_cooldown"] += 1
                continue

            sig = strat.maybe_signal(bar, windows, risk)
            # DEBUG: see bracket as returned by strategy BEFORE OM / sizing touches anything
            if sig:
                br = sig["bracket"]
                entry_px = float(bar["close"])
                stop_px  = float(br.stop_price)
                print(
                    f"[SIG.BRKT] side={sig['order'].side} "
                    f"entry(bar_close)={entry_px:.2f} "
                    f"stop={stop_px:.2f} target={float(br.target_price):.2f} "
                    f"d_stop={abs(entry_px-stop_px):.2f} "
                    f"d_tgt={abs(float(br.target_price)-entry_px):.2f}"
                )
                pass
            strat_used = None
            if sig:
                why["signal"] += 1
                strat_used = strat
            else:
                # Try fallback if primary has no signal
                if fallback is not None:
                    sig = fallback.maybe_signal(bar, windows, risk)
                    if sig:
                        why["signal_fallback"] += 1
                        why[f"signal_fallback_{fallback_name}"] += 1
                        strat_used = fallback
                    else:
                        why["no_signal"] += 1
                        continue
                else:
                    why["no_signal"] += 1
                    continue
            if not sig:
                why["no_signal"] += 1
                continue
            
            # Reject garbage brackets where target ~= entry (often rounds to same tick)
            entry_px = float(getattr(sig["order"], "entry", bar["close"]))
            tgt_px   = float(getattr(sig["bracket"], "target_price", entry_px))
            if abs(tgt_px - entry_px) < float(fees["tick_size"]) * 1.0:
                why["rej_target_too_close"] += 1
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
            if strat_used is not None and hasattr(strat_used, "on_entry_submitted"):
                strat_used.on_entry_submitted(bar.name)

            _mark_entry_for_throttles(strat_used, bar)

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
                    "strat_used": strat_used,
                })
            else:
                om.place_and_simulate(bar, sig["order"])
                if open_position and current_bracket is not None:
                    stop_px = float(getattr(current_bracket, "stop_price", float("nan"))) if current_bracket is not None else float("nan")
                    tgt_px  = float(getattr(current_bracket, "target_price", float("nan"))) if current_bracket is not None else float("nan")
                    close_px = float(bar["close"])
                    high_px = float(bar.get("high", close_px))
                    low_px  = float(bar.get("low", close_px))

                    print(
                        f"[BRKT.DBG] {symbol} side={open_side} "
                        f"entry={float(open_entry_px):.2f} "
                        f"br.stop={stop_px:.2f} br.target={tgt_px:.2f} "
                        f"barL/H/C={low_px:.2f}/{high_px:.2f}/{close_px:.2f}"
                    )

                current_bracket = sig["bracket"]
                open_position = (om.pos.qty != 0)
                open_side = str(om.pos.side).upper()
                open_qty = float(om.pos.qty)
                open_entry_px = float(om.pos.avg_price)
                open_entry_ts = bar["t_local"]
                open_be_locked = False

                sig_entry = float(getattr(sig["order"], "entry", bar["close"]))
                sig_stop  = float(current_bracket.stop_price)
                sig_tgt   = float(current_bracket.target_price)

                stop_px, tgt_px = _reanchor_bracket_to_fill(open_side, sig_entry, sig_stop, sig_tgt, open_entry_px)
                stop_px = _round_to_tick(stop_px, float(fees["tick_size"]))
                tgt_px  = _round_to_tick(tgt_px,  float(fees["tick_size"]))

                current_bracket.entry_price  = float(open_entry_px)
                current_bracket.stop_price   = float(stop_px)
                current_bracket.target_price = float(tgt_px)

                # ✅ risk should be based on ACTUAL fill vs re-anchored stop
                open_risk_pts = max(float(fees["tick_size"]), abs(float(open_entry_px) - float(stop_px)))
                if hasattr(strat_used, "record_entry"):
                    strat_used.record_entry(bar.name)
                #open_risk_pts = risk_pts
                if open_position and getattr(om.pos, "qty", 0) == 0:
                    raise RuntimeError("Entered trade but om.pos.qty is 0 — bracket exits will never trigger.")

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

    # --- EXIT selection (robust, self-validating) ---
    if trades_df.empty or "action" not in trades_df.columns:
        print("[EXIT.SEL] no action col; columns=", list(trades_df.columns))
        exits = trades_df.iloc[0:0].copy()
        mask_exit = None
    else:
        a = trades_df["action"].astype(str).str.strip().str.upper()
        mask_exit = (a.eq("EXIT") | a.str.startswith("EXIT_", na=False)).to_numpy()
        exits = trades_df.loc[mask_exit].copy()
        if "reason" in trades_df.columns:
            exit_reasons = trades_df.loc[a.eq("EXIT"), "reason"].astype(str).value_counts().head(20)
            print("[EXIT.REASONS] top:\n", exit_reasons.to_string())
        #trades_df["action"] = a  # normalize in-place

        print(
            "[EXIT.SEL] mask_true=", int(mask_exit.sum()),
            "rows=", len(trades_df),
            "action_counts=", a.value_counts().head(5).to_dict()
        )

        # HARD ASSERT: if EXIT exists but mask is 0, something is wrong upstream
        if ("EXIT" in set(a.values)) and mask_exit.sum() == 0:
            cols = [c for c in ["ts", "action", "pnl"] if c in trades_df.columns]
            sample = trades_df.loc[a.eq("EXIT").to_numpy(), cols].head(5)
            raise RuntimeError(f"EXIT present but mask_exit.sum()==0. Sample rows:\n{sample}")

    if exits.empty:
        pnl_series = pd.Series([], dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    else:
        ts_col = None
        ts_parsed = None

        for c in ["ts", "timestamp", "exit_time", "time", "dt"]:
            if c not in exits.columns:
                continue
            raw = exits[c]
             # normalize common "fake null" strings
            if raw.dtype == "object":
                raw = raw.astype(str).str.strip()
                raw = raw.replace({"": pd.NA, "NAN": pd.NA, "NaN": pd.NA, "NONE": pd.NA, "None": pd.NA, "NAT": pd.NA, "NaT": pd.NA})    
        
            parsed = pd.to_datetime(raw, utc=True, errors="coerce")
            ok = int(parsed.notna().sum())
            print(f"[EXIT.TS.CAND] {c}: ok={ok}/{len(exits)} dtype={exits[c].dtype}")

            if ok > 0:
                ts_col = c
                ts_parsed = parsed
                break

        if ts_col is None:
            sample_cols = [c for c in ["ts", "timestamp", "exit_time", "time", "dt", "action", "pnl"] if c in exits.columns]
            print("[EXIT.TS.FAIL] sample:\n", exits[sample_cols].head(10))
            raise ValueError("No usable timestamp column found for EXIT rows (all parse to NaT).")

        exits[ts_col] = ts_parsed

        print("[EXIT.TS] using:", ts_col)
        print("[EXIT.TS] NaT count:", int(exits[ts_col].isna().sum()))
        print("[EXIT.TS] min/max:", exits[ts_col].min(), exits[ts_col].max())

        exits = exits.dropna(subset=[ts_col]).sort_values(ts_col)

        pnl_series = (
            exits.set_index(ts_col)["pnl"]
            .astype("float64")
            .groupby(level=0)
            .sum()
            .sort_index()
        )

        print(f"[EXIT] exits={len(exits)} unique_ts={len(pnl_series)} dup_ts={len(exits)-len(pnl_series)}")
        print("[PNL] idx min/max:", pnl_series.index.min(), pnl_series.index.max(), "len:", len(pnl_series))
        print("[PNL.SER] type:", type(pnl_series), "len:", len(pnl_series))
        if len(pnl_series):
            print("[PNL.SER] idx min/max:", pnl_series.index.min(), pnl_series.index.max())
            print("[PNL.SER] head:", pnl_series.head(3).to_dict())

        _pnl_id = id(pnl_series)
        print("[PNL.CHK] id:", id(pnl_series), "same_as_built:", id(pnl_series) == _pnl_id, "len:", len(pnl_series))
        print("[PNL.BUILT] len:", len(pnl_series))

    if args.no_gui:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # safe_sym = symbol.replace("/", "_")
    # logs_dir = Path("logs") / f"bt_{safe_sym}"

    # if getattr(args, "outdir", None):
    #     logs_dir = Path(args.outdir)

    # if getattr(args, "run_tag", None):
    #     logs_dir = logs_dir / str(args.run_tag)

    # logs_dir.mkdir(parents=True, exist_ok=True)

    if pnl_series is not None and not pnl_series.empty:
        ax = pnl_series.plot(title=f"{symbol} Realized PnL per EXIT (USD)")
        fig = ax.get_figure()
        fig.savefig(logs_dir / "pnl_curve.png", dpi=120, bbox_inches="tight")
        if not args.no_gui:
            plt.show(block=False)
            plt.pause(3)
            plt.close()
        plt.close(fig)

    if pnl_series is not None and not pnl_series.empty:
        equity_steps = float(args.starting_equity) + pnl_series.cumsum()

        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("df.index must be a DatetimeIndex to build equity_full")

        equity_full = (
            equity_steps.sort_index()
            .reindex(df.index, method="ffill")
            .fillna(float(args.starting_equity))
        )
    else:
        # fallback if no trades
        equity_full = pd.Series(
            float(args.starting_equity),
            index=df.index if isinstance(df.index, pd.DatetimeIndex) and len(df.index) else [0]
        )
    # Final safety align
    if isinstance(equity_full.index, pd.DatetimeIndex) and isinstance(df.index, pd.DatetimeIndex):
        equity_full = equity_full.reindex(df.index, method="ffill").fillna(float(args.starting_equity))
    
    print("equity_full index min/max:",
          equity_full.index.min() if isinstance(equity_full.index, pd.DatetimeIndex) else None,
          equity_full.index.max() if isinstance(equity_full.index, pd.DatetimeIndex) else None)

    bars_per_day = 1440 if crypto_like else 390
    summary = summarize_equity(equity_full, trades_df, bars_per_day=bars_per_day)

    summary["EndEquity"] = float(equity_full.iloc[-1]) if len(equity_full) else float(args.starting_equity)
    
    print("[PNL.BEFORE_DBG] len:", 0 if pnl_series is None else len(pnl_series))

    summary["PnL_curve_last"] = float(pnl_series.iloc[-1]) if pnl_series is not None and len(pnl_series) else 0.0

    trades_n = int(len(exits))
    summary["Trades"] = trades_n

    if trades_n > 0 and "pnl" in exits.columns:
        pnl_exit = exits["pnl"].astype("float64")
        summary["NetPnL"] = float(pnl_exit.sum())
        summary["WinRate"] = float((pnl_exit > 0).mean() * 100.0)

        gains = float(pnl_exit[pnl_exit > 0].sum())
        losses = float((-pnl_exit[pnl_exit < 0]).sum())
        summary["ProfitFactor"] = (gains / losses) if losses > 0 else float("inf")
    else:
        summary["NetPnL"] = 0.0
        summary["WinRate"] = 0.0
        summary["ProfitFactor"] = 0.0

    print(f"[SUMMARY.CHK] summary.Trades={summary.get('Trades')} summary.NetPnL={summary.get('NetPnL'):.2f}")

    if pnl_series is not None and len(pnl_series):
        equity_tmp = float(args.starting_equity) + pnl_series.cumsum()
        print("equity min/max:" , equity_tmp.min(), equity_tmp.max())
        print("pnl_series len:", len(pnl_series))
        print("exit pnl min:", pnl_series.min())
        print("exit pnl neg count:", (pnl_series < 0).sum())
        print("exit pnl pos count:", (pnl_series > 0).sum())
    else:
        print("pnl_series len: 0 (no exits)")

    # ---- WHY diagnostics (always print top reasons) ----
    top = sorted(why.items(), key=lambda kv: kv[1], reverse=True)[:12]
    print(f"[WHY] {symbol} top reasons:", top)
    # ---- REJ diagnostics (strategy-level rejects) ----
    if hasattr(strat, "_rej"):
        top_rej = sorted(strat._rej.items(), key=lambda kv: kv[1], reverse=True)[:20]
        print(f"[REJ] {symbol} top rejects:", top_rej)

    # Optional: still call out zero/low trade runs explicitly
    trades_ct = int(summary.get("Trades", 0) or 0)
    if trades_ct == 0:
        print(f"[WHY] {symbol} ZERO TRADES — check filters/size gates")
    elif trades_ct <= 3:
        print(f"[WHY] {symbol} LOW TRADES ({trades_ct}) — likely over-filtered")

    # --- Equity sanity: equity_full should reconcile to exits pnl (within rounding) ---
    try:
        netpnl_equity = float(equity_full.iloc[-1] - equity_full.iloc[0])
        netpnl_exits = float(summary["NetPnL"])
        print(f"[SANITY] netpnl(exits)={netpnl_exits:.2f} netpnl(equity)={netpnl_equity:.2f}")
    except Exception as e:
        print(f"[SANITY] netpnl check skipped: {e}")


    if "dist_atr" in df.columns:
        s = df["dist_atr"].dropna()
        print("[DIST_ATR] count:", len(s))
        print("[DIST_ATR] p50 p75 p90 p95 p99:",
            s.quantile([0.50, 0.75, 0.90, 0.95, 0.99]).to_dict())

    if hasattr(strat, "_rej"):
        summary["TopRejects"] = sorted(strat._rej.items(), key=lambda kv: kv[1], reverse=True)[:20]

    write_artifacts(
        equity_full, 
        trades_df, 
        summary, 
        logs_dir=str(logs_dir), 
        write_equity=not args.light_artifacts
    )

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
    ap.add_argument("--no-friction", action="store_true", help="Force comissions/exchange fees/notional fees/slippage to zero")
    ap.add_argument("--fee-bps", type=float, default=None)
    ap.add_argument("--fee-fixed", type=float, default=None)
    ap.add_argument("--slip-bps", type=float, default=None)
    ap.add_argument("--latency-bars", dest="latency_bars", type=int, default=0)
    ap.add_argument("--starting-equity", type=float, default=25000.0)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    ap.add_argument("--max-size", type=int, default=3)
    ap.add_argument("--min-equity", type=float, default=0.0)
    ap.add_argument("--start-date", dest="start_date", default=None, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end-date", dest="end_date", default=None, help="YYYY-MM-DD inclusive")
    ap.add_argument("--outdir", default=None, help="Artifacts output directory")
    ap.add_argument("--run-tag", default=None, help="Subfolder name under outdir (or under default bt_<symbol>)")
    ap.add_argument("--light-artifacts", action="store_true", help="Skip large artifacts (equity_curve.csv)")
    ap.add_argument("--run-id", default=None, help="Unique id for this run (auto if omitted)")
    ap.add_argument("--strict", action="store_true", help="Fail if key inputs are missing or defaulted")


    args = ap.parse_args()
    import uuid
    if not args.run_id:
        args.run_id = uuid.uuid4().hex[:10]
    print(f"[RUN.ID] {args.run_id}")
    # --- Required run context guardrails ---
    if not args.universe:
        raise SystemExit("ERROR: --universe is required (no silent default).")

    if not args.symbol and not args.all:
        raise SystemExit("ERROR: pass --symbol or --all.")

    if not args.csv and not args.csv_dir:
        raise SystemExit("ERROR: pass --csv or --csv-dir (no guessing).")

    # Optional but recommended: force explicit date window for reproducibility
    if not args.start_date or not args.end_date:
        raise SystemExit("ERROR: pass --start-date and --end-date for reproducible runs.")

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



    if args.universe:
        args.universe = _resolve_universe_path(args.universe)
    universe_cfg = _load_yaml_if_exists(args.universe) or {}

    asset_class = (universe_cfg.get("asset_class") or "").lower()

    # --- Build symbol_entries lookup from universe_cfg["symbols"] ALWAYS ---
    symbol_entries: dict[str, dict] = {}

    raw_syms = universe_cfg.get("symbols") or []
    if raw_syms and isinstance(raw_syms[0], dict):
        for entry in raw_syms:
            sym0 = entry.get("symbol")
            if not sym0:
                raise ValueError(f"Universe symbols entries must include 'symbol'. Bad entry={entry}")

            k0 = sym0 if asset_class == "crypto" else str(sym0).upper()

            e0 = dict(entry)
            e0.pop("symbol", None)          # IMPORTANT: don't leak symbol into merge
            symbol_entries[k0] = e0



    def norm_sym(s: str) -> str:
        s = str(s)
        return s if asset_class == "crypto" else s.upper()

    # Decide symbols + build per-symbol entries
    symbols: list[str] = []

    if args.all:
        # Run all symbols listed in universe
        if raw_syms and isinstance(raw_syms[0], dict):
            symbols = list(symbol_entries.keys())
        else:
            symbols = [s if asset_class == "crypto" else str(s).upper() for s in (raw_syms or [])]
        if not symbols:
            raise SystemExit("You passed --all but universe has no symbols.")
    else:
        if not args.symbol:
            raise SystemExit("Pass --symbol <SYM> or use --all with --universe.")
        sym_in = args.symbol if asset_class == "crypto" else str(args.symbol).upper()
        symbols = [sym_in]


    results = []

    base_universe = dict(universe_cfg)
    base_universe.pop("symbols", None)

    for sym in symbols:
        # Choose CSV path
        if args.csv:
            csv_path = args.csv
        else:
            base = args.csv_dir or "data"

            # OPTIONAL: if you want per-symbol timeframe naming later, support it here.
            # Example: look for per-sym override: backfill.timeframe == "15m" and name accordingly.
            per_sym_cfg = symbol_entries.get(sym, {})
            tf = None
            backfill = per_sym_cfg.get("backfill") or base_universe.get("backfill") or {}
            if isinstance(backfill, dict):
                tf = backfill.get("timeframe")

            csv_sym = _symbol_to_csv_stem(sym)
            # keep your current behavior by default:
            suffix = "1m"
            # If you ever decide to separate files by timeframe:
            # if tf in ("15m", "1h", "5m"): suffix = tf

            csv_path = str(Path(base) / f"{csv_sym}_live_{suffix}.csv")

        per_sym = symbol_entries.get(sym, {})  # {} if old string list format
        merged_universe_cfg = deep_merge(base_universe, per_sym)
        merged_universe_cfg["symbol"] = sym

        r = run_one_symbol(
            args=args,
            symbol=sym,
            csv_path=csv_path,
            universe_cfg=merged_universe_cfg,
        )
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
