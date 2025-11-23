# run_backtest.py
import yaml, pandas as pd, argparse, os

from trader.engine.backtest import prepare_bars, equity_curve
from trader.engine.execution import OrderManager
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
from trader.engine.latency import BarDelay
from trader.engine.metrics_ext import summarize_equity, write_artifacts
import matplotlib

def _compute_size_from_equity(symbol, equity, risk_pct, stop_ticks, tick_value, last_price, max_size):
    # equities / ETF path
    if symbol in ("SPY", "QQQ"):
        if last_price <= 0:
            return 0
        # how many shares can I afford?
        affordable = int(equity // last_price)
        return max(0, min(affordable, max_size))

    # futures-style path
    risk_capital = equity * risk_pct
    if not stop_ticks or stop_ticks <= 0 or not tick_value:
        # fallback: 1, but respect max_size
        return max(0, min(1, max_size))

    risk_per_contract = stop_ticks * tick_value
    if risk_per_contract <= 0:
        return max(0, min(1, max_size))

    raw = int(risk_capital // risk_per_contract)
    return max(0, min(raw, max_size))


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
    return 9999  # not inside any window

def _minutes_between(a_str, b_str):
    try:
        ah, am = map(int, str(a_str)[:5].split(":"))
        bh, bm = map(int, str(b_str)[:5].split(":"))
        return (bh*60+bm) - (ah*60+am)
    except Exception:
        return 0


def main(args):
    # ---- Load layered config: profile -> env -> extra ----
    cfg = {}
    if args.profile and os.path.exists(args.profile):
        cfg = _deep_merge(cfg, yaml.safe_load(open(args.profile, "r")) or {})
    if args.env and os.path.exists(args.env):
        cfg = _deep_merge(cfg, yaml.safe_load(open(args.env, "r")) or {})
    if args.config and os.path.exists(args.config):
        cfg = _deep_merge(cfg, yaml.safe_load(open(args.config, "r")) or {})

    # ---- Contracts (fees/exchange/ticks) ----
    contracts = yaml.safe_load(open(args.contracts, "r"))
    ct = contracts[args.symbol]
    cfg.setdefault("fees", {})
    for k in ("tick_size", "tick_value", "commission_per_contract", "exchange_fees_per_contract"):
        if k in ct:
            cfg["fees"][k] = ct[k]
    cfg["symbol"] = ct.get("symbol", args.symbol)

    # ---- Data path (CLI beats YAML) ----
    csv_path = args.csv or ((cfg.get("data") or {}).get("csv_path"))
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"CSV not found. Pass --csv or set data.csv_path in config. Got: {csv_path}"
        )
    equity = args.starting_equity

    # ---- Prep bars ----
    tz = cfg.get("timezone", "America/Chicago")
    orb_min = cfg["strategy"]["orb_minutes"]
    df = pd.read_csv(csv_path)
    df = prepare_bars(df, tz, orb_min)

    # ---- Risk / fees ----
    fees = cfg["fees"]
    yaml_risk = cfg["risk"]
    
    risk = RiskGovernor(RiskConfig(
        account_equity=yaml_risk["account_equity"],
        risk_pct=args.risk_pct or yaml_risk["risk_pct"],
        max_daily_loss_R=yaml_risk["max_daily_loss_R"],
        max_consec_losses=yaml_risk["max_consec_losses"],
        tick_value=fees["tick_value"],
        flat_time=yaml_risk["flat_time"],
        news_lockout_minutes=yaml_risk["news_lockout_minutes"],
    ))
    risk.risk_buffer_pct = float(cfg.get("risk", {}).get("risk_buffer_pct", 1.0))
    risk.max_contracts_per_trade = int(cfg["risk"].get("max_contracts_per_trade", 0)) or None

    base_eq = args.starting_equity

    dpp = fees["tick_value"] / fees["tick_size"]  # dollars per full point
    om = OrderManager(
        commission_per_contract=fees["commission_per_contract"],
        exchange_fees_per_contract=fees["exchange_fees_per_contract"],
        tick_size=fees["tick_size"],
        fee_bps=args.fee_bps,
        fee_fixed=args.fee_fixed,
        slip_bps=args.slip_bps,
        dollars_per_point=dpp,
    )
    delay = BarDelay(bars=args.latency_bars or 0)

    # ---- Strategy ----
    sc = cfg["strategy"]
    strat = HybridOrbVwap(StratConfig(
        orb_minutes=sc["orb_minutes"],
        atr_len=sc["atr_len"],
        break_eps_ticks=sc["break_eps_ticks"],
        atr_mult=sc["atr_mult"],
        target_R=sc["target_R"],
        slope_min=sc.get("slope_min", 0.0),
        stop_pad_ticks=sc["stop_pad_ticks"],
        trail_pad_ticks=sc["trail_pad_ticks"],
        tick_size=fees["tick_size"],
        tick_value=fees["tick_value"],
    ))
    windows = _norm_windows(sc.get("session_windows", []))
    # ---- Execution knobs (from YAML 'execution') ----
    exe = cfg.setdefault("execution", {})
    BE_LOCK_MIN      = int(exe.get("be_lock_min", 12))
    BE_REQ_TICKS     = int(exe.get("be_req_profit_ticks", 2))
    MAX_HOLD_MIN     = int(exe.get("max_hold_min", 40))
    HARD_CUTOFF_R    = float(exe.get("hard_cutoff_R", -0.25))
    HARD_CUTOFF_MIN  = int(exe.get("hard_cutoff_min", 10))
    MIN_WIN_END      = int(exe.get("min_minutes_to_window_end", 20))

    print(
        f"[EXEC KNOBS] "
        f"BE_LOCK_MIN={BE_LOCK_MIN}  "
        f"BE_REQ_TICKS={BE_REQ_TICKS}  "
        f"MAX_HOLD_MIN={MAX_HOLD_MIN}  "
        f"HARD_CUTOFF_R={HARD_CUTOFF_R}  "
        f"HARD_CUTOFF_MIN={HARD_CUTOFF_MIN}  "
        f"MIN_WIN_END={MIN_WIN_END}"
        )

    # ---- Sim loop ----
    open_position = False
    current_bracket = None
    prev_date = None
    open_side = None
    open_qty = 0
    open_entry_px = None
    open_entry_ts = None
    open_be_locked = False
    open_risk_pts = None

    for i, (_, bar) in enumerate(df.iterrows()):
        # new day
        if prev_date is None or bar["date"] != prev_date:
            risk.reset_day(base_eq + om.realized_pnl)
            prev_date = bar["date"]
            open_position = False
            current_bracket = None
            open_side = None
            open_qty = 0
            open_entry_px = None
            open_entry_ts = None
            open_be_locked = False
            open_risk_pts = None

        # latency queue
        for due in delay.due(i):
            # unwrap payload
            if isinstance(due, dict):
                order   = due["order"]
                bracket = due.get("bracket")
                entry   = float(due.get("entry", getattr(order, "entry", bar["close"])))
            else:
                # backward-compat: if you ever pushed a bare order
                order   = due
                bracket = None
                entry   = float(getattr(order, "entry", bar["close"]))

            # if something is somehow already open, skip this due order
            if open_position:
                continue

            # place and mark state as OPEN
            om.place_and_simulate(bar, order)
            current_bracket = bracket if bracket is not None else current_bracket
            open_position   = True
            open_side       = str(order.side).upper()
            open_qty        = int(order.qty)
            open_entry_px   = entry
            open_entry_ts   = bar["t_local"]
            open_be_locked  = False


        # outside session => flatten-on-close simulation
        if not risk.can_trade_now(bar["t_local"]):
            if open_position and current_bracket:
                before = len(om.trades)
                exit_info = om.simulate_bracket(bar, current_bracket)
                exited = bool(exit_info) or (len(om.trades) > before and om.trades[-1].get("action") == "EXIT")

                if not exited:
                    exit_px = float(bar["close"])
                    side = (open_side or "BUY")
                    qty = int(open_qty or getattr(current_bracket, "qty", 0) or 0)
                    entry_px = float(open_entry_px if open_entry_px is not None else bar["close"])
                    dpp = float(fees["tick_value"]) / float(fees["tick_size"])
                    pnl = (exit_px - entry_px) * dpp * (1.0 if side == "BUY" else -1.0)

                    om.trades.append({
                        "t_local": bar["t_local"],
                        "action": "EXIT",
                        "side": side,
                        "qty": qty,
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
                            try:
                                om.trades[-1]["R"] = float(R)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    equity += pnl
            # reset open state either way
            open_position = False
            current_bracket = None
            open_side = None
            open_qty = 0
            open_entry_px = None
            open_entry_ts = None
            open_be_locked = False
            open_risk_pts = None
            continue

        # --- manage OPEN position ---

        # 1a) Consume normal STOP/TARGET exits first via the bracket
        if open_position and current_bracket:
            before = len(om.trades)
            exit_info = om.simulate_bracket(bar, current_bracket)

            # If simulate_bracket appended an EXIT but returned None, synthesize exit_info
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

                # clear state
                open_position = False
                current_bracket = None
                open_side = None
                open_qty = 0
                open_entry_px = None
                open_entry_ts = None
                open_be_locked = False
                continue

        # 1b) Guarded BE lock: only after BE_LOCK_MIN minutes AND after price moved ≥ BE_REQ_TICKS in our favor
        if open_position and current_bracket and open_entry_ts is not None and not open_be_locked:
            if _minutes_between(open_entry_ts, bar["t_local"]) >= BE_LOCK_MIN:
                tick = float(fees["tick_size"])
                entry_px = float(open_entry_px)
                px_now  = float(bar["close"])

                moved_favorably = (
                    (open_side == "BUY"  and px_now >= entry_px + BE_REQ_TICKS * tick) or
                    (open_side == "SELL" and px_now <= entry_px - BE_REQ_TICKS * tick)
                )

                if moved_favorably:
                    if open_side == "BUY":
                        current_bracket.stop_price = max(float(current_bracket.stop_price), entry_px)
                    else:
                        current_bracket.stop_price = min(float(current_bracket.stop_price), entry_px)
                    open_be_locked = True
                    # no trade appended; bracket will realize BE later if hit

        # 1c) Time-based exit once MAX_HOLD_MIN is exceeded
        if open_position and current_bracket and open_entry_ts is not None:
            if _minutes_between(open_entry_ts, bar["t_local"]) >= MAX_HOLD_MIN:
                before = len(om.trades)
                exit_info = om.simulate_bracket(bar, current_bracket)
                exited = bool(exit_info) or (len(om.trades) > before and om.trades[-1].get("action") == "EXIT")

                if not exited:
                    exit_px = float(bar["close"])
                    side = (open_side or "BUY")
                    qty = int(open_qty or getattr(current_bracket, "qty", 0) or 0)
                    entry_px = float(open_entry_px if open_entry_px is not None else bar["close"])
                    dpp = float(fees["tick_value"]) / float(fees["tick_size"])
                    pnl = (exit_px - entry_px) * dpp * (1.0 if side == "BUY" else -1.0)

                    om.trades.append({
                        "t_local": bar["t_local"],
                        "action": "EXIT",
                        "side": side,
                        "qty": qty,
                        "price": entry_px,
                        "reason": "TIME",
                        "exit_price": exit_px,
                        "pnl": pnl,
                        "fees_total": 0.0,
                    })
                    
                    # NEW: record R for the TIME exit using the stored initial risk
                    try:
                        if open_risk_pts and float(open_risk_pts) > 0:
                            pnl_pts = (exit_px - entry_px) * (1.0 if side == "BUY" else -1.0)
                            R = pnl_pts / float(open_risk_pts)
                            risk.record_trade_outcome_R(R)
                            om.trades[-1]["R"] = float(R)   # stash R on the row for auditing
                            
                    except Exception:
                        pass
                    equity += pnl
                # clear state
                open_position = False
                current_bracket = None
                open_side = None
                open_qty = 0
                open_entry_px = None
                open_entry_ts = None
                open_be_locked = False
                continue

        # new signal
        if not open_position and not risk.halted:
            sig = strat.maybe_signal(bar, windows, risk)
            if sig:
                # skip entries too close to a window close
                if _minutes_to_window_end(bar["t_local"], windows) < MIN_WIN_END:
                    continue

                tick_size = float(fees["tick_size"])
                entry = float(getattr(sig["order"], "entry", bar["close"]))
                stop  = float(sig["bracket"].stop_price)

                # enforce a 1-tick minimum risk distance
                if abs(entry - stop) < tick_size:
                    if str(sig["order"].side).upper() == "BUY":
                        sig["bracket"].stop_price = entry - tick_size
                    else:
                        sig["bracket"].stop_price = entry + tick_size
                    stop = float(sig["bracket"].stop_price)

                # === CAPITAL-AWARE SIZING ===
                if equity < args.min_equity:
                    # can't trade, too low
                    continue

                # how many ticks of risk?
                stop_ticks = abs(entry - stop) / tick_size if tick_size else 0
                size = _compute_size_from_equity(
                    symbol=args.symbol,
                    equity=equity,
                    risk_pct=args.risk_pct,
                    stop_ticks=stop_ticks,
                    tick_value=fees["tick_value"],
                    last_price=bar["close"],
                    max_size=args.max_size,
                )
                if size < 1:
                    # can't afford it
                    continue

                # override the order/bracket qty to what we computed
                sig["order"].qty = size
                if hasattr(sig["bracket"], "qty"):
                    sig["bracket"].qty = size

                if delay.bars > 0:
                    delay.submit(i, sig["order"])
                else:
                    om.place_and_simulate(bar, sig["order"])
                    current_bracket = sig["bracket"]
                    open_position = True
                    open_side = str(sig["order"].side).upper()
                    open_qty = int(sig["order"].qty)
                    open_entry_px = entry
                    open_entry_ts = bar["t_local"]
                    open_risk_pts = max(tick_size, abs(entry - stop))

    # ---- Results ----
    cleaned = []
    open_flag = False
    for tr in om.trades:
        act = tr.get("action")
        if act == "ENTRY":
            if not open_flag:
                cleaned.append(tr)
                open_flag = True
            else:
                # already open; ignore duplicate ENTRY
                pass
        elif act == "EXIT":
            if open_flag:
                cleaned.append(tr)
                open_flag = False
            else:
                # stray EXIT without an open position; drop it
                pass
        else:
            cleaned.append(tr)
    om.trades = cleaned

    curve = equity_curve(om.trades)  # cumulative PnL series
    pnl_series = curve
    
    final_equity = float(equity)
    if len(df.index) > 0:
        equity_series = pd.Series([final_equity], index=[df.index[-1]])
        equity_full = pd.Series(index=df.index, dtype=float)
        equity_full.iloc[0] = args.starting_equity
        equity_full = equity_full.ffill().fillna(args.starting_equity)
        equity_full.iloc[-1] = final_equity
    else:
        equity_series = pd.Series([final_equity])
        equity_full = equity_series

    if not pnl_series.empty:
        total_pnl = float(pnl_series.iloc[-1])
    else: 
        total_pnl = 0.0
    winners = [t for t in om.trades if t.get("action") == "EXIT" and t.get("pnl", 0) > 0]
    losers  = [t for t in om.trades if t.get("action") == "EXIT" and t.get("pnl", 0) <= 0]
    hit_rate = (len(winners) / max(1, len(winners) + len(losers))) * 100.0
    print(f"Trades: {len(winners)+len(losers)}  |  Hit Rate: {hit_rate:.1f}%  |  Total PnL: ${total_pnl:,.2f}")

    os.makedirs("logs", exist_ok=True)
    trades_df = pd.DataFrame(om.trades)

    # ensure consistent columns even if no trades
    if trades_df.empty:
        trades_df = pd.DataFrame(columns=[
            "ts","t_local","action","side","qty","entry_price","price",
            "reason","exit_price","pnl","fees_total","R"
        ])

    trades_df.to_csv("logs/backtest_trades.csv", index=False)
    print("\nSaved trades to logs/backtest_trades.csv")
    print(trades_df.head(10))


    # --- Exit-based sanity metrics (safe for zero-trade runs) ---
    exits = None
    if not trades_df.empty and "action" in trades_df.columns:
        exits = trades_df.loc[trades_df["action"] == "EXIT"].copy()

    extra_summary = {}
    if exits is not None and not exits.empty:
        dollar_expectancy = float(exits["pnl"].mean())
        pf_exit = float(
            exits.loc[exits["pnl"] > 0, "pnl"].sum() /
            max(1, -exits.loc[exits["pnl"] <= 0, "pnl"].sum())
        )
        wins = int((exits["pnl"] > 0).sum())
        losses = int((exits["pnl"] <= 0).sum())
        winrate_exit = wins / max(1, wins + losses)

        print(f"\n[EXIT METRICS] N={len(exits)}  Win%={winrate_exit:.1%}  PF={pf_exit:.2f}  $Exp/exit={dollar_expectancy:.2f}")
        extra_summary["PF_ExitBased"] = pf_exit
        extra_summary["DollarExpectancyPerExit"] = dollar_expectancy
    else:
        print("\n[EXIT METRICS] No exits this run (likely too few bars or outside session windows).")
      

    # --- Plots ---
    if args.no_gui:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not pnl_series.empty:
        ax = pnl_series.plot(title="PnL (USD)")
        fig = ax.get_figure()
        fig.savefig("logs/pnl_curve.png", dpi=120, bbox_inches="tight")
        print("Saved chart to logs/pnl_curve.png")
        if not args.no_gui:
            plt.show()
        plt.close(fig)

    if not equity_full.empty:
        ax2 = equity_full.plot(title="Equity (USD)")
        fig2 = ax2.get_figure()
        fig2.savefig("logs/equity_curve.png", dpi=120, bbox_inches="tight")
        if not args.no_gui:
            plt.show()
        plt.close(fig2)


    # --- Summary & artifacts (attach exit metrics here so they land in summary.json) ---
    summary = summarize_equity(equity_full, trades_df)
    if extra_summary:
        summary.update(extra_summary)

    summary["EndEquity"] = float(equity)

    write_artifacts(equity_full, trades_df, summary, logs_dir="logs")

    print("\n=== SUMMARY ===")
    print(f"Trades: {summary.get('Trades', 0)} | WinRate: {summary.get('WinRate', 0):.1f}% "
          f"| PF: {summary.get('ProfitFactor')} | Expectancy: ${summary.get('Expectancy', 0):.2f}")
    print(f"NetPnL: ${summary.get('NetPnL', 0):.2f} | Fees: ${summary.get('FeesTotal', 0):.2f} "
          f"| NetRet: {summary.get('NetReturnPct', 0):.2f}% | MDD: {summary.get('MaxDrawdown', 0):.2%}")
    print("Saved logs/summary.json")



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts", default="config/contracts.yaml")
    ap.add_argument("--profile", default="config/profile/futures.yaml")
    ap.add_argument("--env", default=None, help="Optional env yaml to merge")
    ap.add_argument("--config", default=None, help="Optional extra yaml to merge last")
    ap.add_argument("--symbol", default="MES", choices=["ES", "MES", "MNQ", "SPY", "QQQ"])
    ap.add_argument("--csv", dest="csv", help="CSV path for backtest (overrides config.data.csv_path)")
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--latency-bars", type=int, default=0)
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--fee-fixed", type=float, default=0.0)
    ap.add_argument("--slip-bps", type=float, default=0.5)
    ap.add_argument("--kill-dd", type=float, default=0.15)   # kept for parity; not wired unless your RiskGovernor reads it
    ap.add_argument("--kill-day", type=float, default=0.05)
    ap.add_argument("--starting-equity", type=float, default=25000.0, help="starting account equity for capital-aware backtests")
    ap.add_argument("--risk-pct", type=float, default=0.01, help="fraction of equity to risk per trade (futures path)")
    ap.add_argument("--max-size", type=int, default=3, help="hard cap on contracts/shares")
    ap.add_argument("--min-equity", type=float, default=0.0, help="skip trades if equity drops below this")  # same note
    args = ap.parse_args()
    main(args)
