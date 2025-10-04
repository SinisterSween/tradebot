# run_backtest.py
import yaml, pandas as pd, argparse, os

from trader.engine.backtest import prepare_bars, equity_curve
from trader.engine.execution import OrderManager
from trader.engine.risk import RiskConfig, RiskGovernor
from trader.strategies.hybrid_orb_vwap import HybridOrbVwap, StratConfig
from trader.engine.latency import BarDelay
from trader.engine.metrics_ext import summarize_equity, write_artifacts
import matplotlib

MAX_HOLD_MIN = 26
BE_LOCK_MIN  = 14

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

    # ---- Prep bars ----
    tz = cfg.get("timezone", "America/Chicago")
    orb_min = cfg["strategy"]["orb_minutes"]
    df = pd.read_csv(csv_path)
    df = prepare_bars(df, tz, orb_min)

    # ---- Risk / fees ----
    fees = cfg["fees"]
    base_eq = cfg["risk"]["account_equity"]
    risk = RiskGovernor(RiskConfig(
        account_equity=cfg["risk"]["account_equity"],
        risk_pct=cfg["risk"]["risk_pct"],
        max_daily_loss_R=cfg["risk"]["max_daily_loss_R"],
        max_consec_losses=cfg["risk"]["max_consec_losses"],
        tick_value=fees["tick_value"],
        flat_time=cfg["risk"]["flat_time"],
        news_lockout_minutes=cfg["risk"]["news_lockout_minutes"],
    ))
    risk.risk_buffer_pct = float(cfg.get("risk", {}).get("risk_buffer_pct", 1.0))
    risk.max_contracts_per_trade = int(cfg["risk"].get("max_contracts_per_trade", 0)) or None

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

    # ---- Sim loop ----
    open_position = False
    current_bracket = None
    prev_date = None
    open_side = None
    open_qty = 0
    open_entry_px = None
    open_entry_ts = None
    open_be_locked = False

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

            # reset open state either way
            open_position = False
            current_bracket = None
            open_side = None
            open_qty = 0
            open_entry_px = None
            open_entry_ts = None
            open_be_locked = False
            continue





        # --- manage OPEN position ---

        # 1) Normal STOP/TARGET exits first
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

                open_position = False
                current_bracket = None
                open_side = None
                open_qty = 0
                open_entry_px = None
                open_entry_ts = None
                open_be_locked = False
                continue

            # 1b) Break-even scratch after BE_LOCK_MIN minutes, at +/− one tick (and pay exit fees)
            if open_entry_ts is not None and not open_be_locked:
                if _minutes_between(open_entry_ts, bar["t_local"]) >= BE_LOCK_MIN:
                    tick = float(fees["tick_size"])
                    exit_px = float(open_entry_px) + (tick if open_side == "BUY" else -tick)
                    qty = int(open_qty or getattr(current_bracket, "qty", 0) or 0)

                    # dollars-per-point
                    dpp = float(fees["tick_value"]) / float(fees["tick_size"])

                    # CHARGE EXIT FEES for the BE close
                    fee_per_ct = float(fees.get("commission_per_contract", 0.0)) + float(fees.get("exchange_fees_per_contract", 0.0))
                    fees_exit = fee_per_ct * qty

                    # BE PnL: tiny price edge minus exit fees
                    pnl = (exit_px - float(open_entry_px)) * dpp * (1.0 if open_side == "BUY" else -1.0) - fees_exit

                    om.trades.append({
                        "t_local": bar["t_local"],
                        "action": "EXIT",
                        "side": open_side,
                        "qty": qty,
                        "price": float(open_entry_px),   # entry
                        "reason": "BE",
                        "exit_price": exit_px,
                        "pnl": pnl,
                        "fees_total": fees_exit,
                    })
                    # neutral R for expectancy in R; still pays fees in dollars
                    risk.record_trade_outcome_R(0.0)

                    # clear state
                    open_position = False
                    current_bracket = None
                    open_side = None
                    open_qty = 0
                    open_entry_px = None
                    open_entry_ts = None
                    open_be_locked = False
                    continue


        # 2) Time-based exit to avoid WINDOW later
        if open_position and current_bracket:
            if open_entry_ts is not None and _minutes_between(open_entry_ts, bar["t_local"]) >= MAX_HOLD_MIN:
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
                if _minutes_to_window_end(bar["t_local"], windows) < 20:
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

                if delay.bars > 0:
                    payload = {
                        "order":   sig["order"],
                        "bracket": sig["bracket"],
                        "entry":   entry,  # numeric entry price we computed
                    }
                    delay.submit(i, payload) 
                else:
                    # immediate; mark open now
                    om.place_and_simulate(bar, sig["order"])
                    current_bracket = sig["bracket"]
                    open_position = True
                    open_side = str(sig["order"].side).upper()
                    open_qty = int(sig["order"].qty)
                    open_entry_px = entry
                    open_entry_ts = bar["t_local"]
                    open_be_locked = False



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
    equity_series = base_eq + pnl_series

    if isinstance(equity_series.index, pd.DatetimeIndex) and isinstance(df.index, pd.DatetimeIndex):
        equity_full = equity_series.reindex(df.index, method="ffill").fillna(base_eq)
    else:
        equity_full = equity_series

    total_pnl = float(pnl_series.iloc[-1]) if not pnl_series.empty else 0.0
    winners = [t for t in om.trades if t.get("action") == "EXIT" and t.get("pnl", 0) > 0]
    losers  = [t for t in om.trades if t.get("action") == "EXIT" and t.get("pnl", 0) <= 0]
    hit_rate = (len(winners) / max(1, len(winners) + len(losers))) * 100.0
    print(f"Trades: {len(winners)+len(losers)}  |  Hit Rate: {hit_rate:.1f}%  |  Total PnL: ${total_pnl:,.2f}")

    os.makedirs("logs", exist_ok=True)
    trades_df = pd.DataFrame(om.trades)
    trades_df.to_csv("logs/backtest_trades.csv", index=False)
    print("\nSaved trades to logs/backtest_trades.csv")
    print(trades_df.head(10))

    # Plots
    
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

    summary = summarize_equity(equity_full, trades_df)
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
    ap.add_argument("--symbol", default="MES", choices=["ES", "MES", "SPY", "QQQ"])
    ap.add_argument("--csv", dest="csv", help="CSV path for backtest (overrides config.data.csv_path)")
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--latency-bars", type=int, default=0)
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--fee-fixed", type=float, default=0.0)
    ap.add_argument("--slip-bps", type=float, default=0.5)
    ap.add_argument("--kill-dd", type=float, default=0.15)   # kept for parity; not wired unless your RiskGovernor reads it
    ap.add_argument("--kill-day", type=float, default=0.05)  # same note
    args = ap.parse_args()
    main(args)
