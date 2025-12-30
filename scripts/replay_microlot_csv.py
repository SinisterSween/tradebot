#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Offline CSV replay "microlot" tool with a StrategyAdapter.

Usage examples:

  PYTHONPATH=. python scripts/replay_microlot_csv.py \
      --csv data/MES_live_1m.csv \
      --symbol MES

  PYTHONPATH=. python scripts/replay_microlot_csv.py \
      --csv data/MES_live_1m.csv \
      --symbol MES \
      --cash 5000 \
      --fast 10 \
      --slow 30 \
      --trade-csv logs/mes_replay_trades.csv
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd


# -----------------------------
# Data structures
# -----------------------------

@dataclass
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_px: float
    exit_px: float
    qty: float
    pnl: float

    @property
    def ret_pct(self) -> float:
        if self.entry_px == 0:
            return 0.0
        direction = 1 if self.qty > 0 else -1
        return direction * (self.exit_px - self.entry_px) / self.entry_px * 100.0


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    entry_px: float = 0.0
    entry_time: Optional[pd.Timestamp] = None


# -----------------------------
# Replay broker
# -----------------------------

class ReplayBroker:
    """
    Tiny in-memory broker:
    - Single-symbol long-only by default.
    - Market "fills" at provided bar price.
    """

    def __init__(self, symbol: str, starting_cash: float):
        self.symbol = symbol
        self.cash = float(starting_cash)
        self.position = Position(symbol=symbol, qty=0.0, entry_px=0.0, entry_time=None)
        self.trades: List[Trade] = []

    # ---- Public API the strategy can use ----

    def market_buy(self, ts: pd.Timestamp, price: float, qty: float):
        """
        Simple model: close any existing position, then open a new long.
        """
        if qty == 0:
            return

        # Close old position first
        if self.position.qty != 0:
            self._close_position(ts, price)

        cost = price * qty
        self.cash -= cost
        self.position = Position(symbol=self.symbol, qty=qty, entry_px=price, entry_time=ts)

    def flat(self, ts: pd.Timestamp, price: float):
        """Go to flat at this bar's price."""
        self._close_position(ts, price)

    def get_position(self) -> Position:
        return self.position

    def finalize(self, ts: pd.Timestamp, last_price: float):
        """Force-close any open position at the end of replay."""
        self._close_position(ts, last_price)

    def equity(self, last_price: float) -> float:
        if self.position.qty == 0:
            return self.cash
        return self.cash + self.position.qty * last_price

    # ---- Internal helpers ----

    def _close_position(self, ts: pd.Timestamp, price: float):
        if self.position.qty == 0:
            return

        qty = self.position.qty
        entry_px = self.position.entry_px
        entry_time = self.position.entry_time or ts

        direction = 1 if qty > 0 else -1
        pnl = direction * (price - entry_px) * abs(qty)

        self.cash += pnl
        self.trades.append(
            Trade(
                symbol=self.symbol,
                entry_time=entry_time,
                exit_time=ts,
                entry_px=entry_px,
                exit_px=price,
                qty=qty,
                pnl=pnl,
            )
        )

        # Reset to flat
        self.position = Position(symbol=self.symbol, qty=0.0, entry_px=0.0, entry_time=None)


# -----------------------------
# Strategy adapter
# -----------------------------

class StrategyAdapter:
    """
    Adapter layer between the replay loop and your strategy.

    Modes:
      - "ma": built-in fast/slow MA cross (placeholder / toy).
      - "real": hook for your actual live strategy (HybridOrbVwap/microlot).

    For now, the replay script still uses "ma" internally. When you're ready
    to wire in your real strategy, you will:
      1) change `mode` default to "real" (or add a CLI flag),
      2) implement `_init_real_strategy` and `_on_bar_real`.
    """

    def __init__(self, symbol: str, fast: int, slow: int, trade_qty: float, mode: str = "ma"):
        if fast >= slow:
            raise ValueError("fast MA length must be < slow MA length")

        self.symbol = symbol
        self.fast = fast
        self.slow = slow
        self.trade_qty = trade_qty
        self.mode = mode

        # --- MA-cross state (toy strategy) ---
        self._closes: List[float] = []
        self._last_regime: Optional[int] = None  # 1 = long, 0 = flat

        # --- Real strategy state (to be wired to your existing code) ---
        self._real_strategy = None
        if self.mode == "real":
            self._init_real_strategy()

    # ------------- entrypoint used by replay loop -------------

    def on_bar(self, ts: pd.Timestamp, row: pd.Series, broker: ReplayBroker):
        if self.mode == "ma":
            return self._on_bar_ma(ts, row, broker)
        elif self.mode == "real":
            return self._on_bar_real(ts, row, broker)
        else:
            raise ValueError(f"Unknown StrategyAdapter mode: {self.mode!r}")

    # ------------- MA-cross placeholder implementation -------------

    def _on_bar_ma(self, ts: pd.Timestamp, row: pd.Series, broker: ReplayBroker):
        price = float(row["close"])
        self._closes.append(price)

        # not enough data to compute MAs yet
        if len(self._closes) < self.slow:
            return

        fast_ma = float(np.mean(self._closes[-self.fast:]))
        slow_ma = float(np.mean(self._closes[-self.slow:]))

        regime = 1 if fast_ma > slow_ma else 0  # 1 = want to be long, 0 = want to be flat
        pos = broker.get_position()

        # Simple regime logic:
        # - If regime wants long and we're flat -> buy
        # - If regime wants flat and we're long -> flat
        if regime == 1 and pos.qty == 0:
            broker.market_buy(ts, price, self.trade_qty)
        elif regime == 0 and pos.qty != 0:
            broker.flat(ts, price)

        self._last_regime = regime

    # ------------- Real strategy hook (to wire your live logic) -------------

    def _init_real_strategy(self):
        """
        This is where you construct your real strategy object.

        Rough sketch (you will replace this with actual imports / config):

            from trader.strategies.hybrid_orb_vwap import HybridOrbVwap
            from trader.engine.risk import RiskGovernor

            risk = RiskGovernor(config=..., logger=...)
            self._real_strategy = HybridOrbVwap(
                symbol=self.symbol,
                risk=risk,
                config=...,
                logger=...,
            )

        For now this raises, so the script keeps using "ma" mode.
        """
        raise NotImplementedError(
            "Real strategy mode is not wired yet. "
            "Implement _init_real_strategy() with your HybridOrbVwap/microlot setup."
        )

    def _on_bar_real(self, ts: pd.Timestamp, row: pd.Series, broker: ReplayBroker):
        """
        Call into your actual live strategy here.

        You'll adapt this to whatever API your strategy uses. Common patterns:

            bar = {
                'ts': ts,
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row.get('volume', 0)),
            }

            self._real_strategy.on_bar(bar, broker=broker)

        For now, this just raises to make it obvious you need to wire it.
        """
        if self._real_strategy is None:
            raise RuntimeError("Real strategy not initialized. Did _init_real_strategy() run?")
        raise NotImplementedError(
            "_on_bar_real is a placeholder. "
            "Wire this to your actual strategy's on_bar/handle_bar API."
        )



# -----------------------------
# Replay loop
# -----------------------------

def run_replay(
    csv_path: Path,
    symbol: str,
    cash: float,
    fast: int,
    slow: int,
    trade_qty: float,
    trade_csv: Optional[Path] = None,
    tz: Optional[str] = None,
) -> None:
    # Load CSV
    df = pd.read_csv(csv_path)

    # Time column detection
    time_col_candidates = [c for c in df.columns if c.lower() in ("timestamp", "time", "datetime", "date")]
    if not time_col_candidates:
        raise ValueError(
            f"Could not find a timestamp column in {df.columns.tolist()}. "
            "Expected one of: timestamp, time, datetime, date."
        )
    ts_col = time_col_candidates[0]

    # Parse timestamps and sort
    df[ts_col] = pd.to_datetime(df[ts_col])
    df = df.sort_values(ts_col).reset_index(drop=True)

    broker = ReplayBroker(symbol=symbol, starting_cash=cash)
    strategy = StrategyAdapter(symbol=symbol, fast=fast, slow=slow, trade_qty=trade_qty)

    # Replay bar-by-bar
    for _, row in df.iterrows():
        ts = row[ts_col]
        if tz:
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC").tz_convert(tz)
            else:
                ts = ts.tz_convert(tz)

        strategy.on_bar(ts, row, broker)

    # Finalize (close leftover position)
    last_ts = df[ts_col].iloc[-1]
    last_close = float(df["close"].iloc[-1])
    broker.finalize(last_ts, last_close)

    # -----------------
    # Stats + output
    # -----------------
    trades = broker.trades
    equity_final = broker.equity(last_close)
    equity_return_pct = (equity_final - cash) / cash * 100.0

    print(f"\n=== CSV Microlot Replay: {symbol} ===")
    print(f"CSV: {csv_path}")
    print(f"Bars: {len(df)}")
    print(f"Starting cash: {cash:,.2f}")
    print(f"Final equity:  {equity_final:,.2f} ({equity_return_pct:+.2f}%)")
    print(f"Trades: {len(trades)}")

    if trades:
        pnl = np.array([t.pnl for t in trades])
        win_mask = pnl > 0
        win_rate = win_mask.mean() * 100.0
        avg_win = pnl[win_mask].mean() if win_mask.any() else 0.0
        avg_loss = pnl[~win_mask].mean() if (~win_mask).any() else 0.0
        pf = (pnl[win_mask].sum() / abs(pnl[~win_mask].sum())) if (win_mask.any() and (~win_mask).any()) else np.nan

        print(f"Win rate: {win_rate:.1f}%")
        print(f"Avg win:  {avg_win:,.2f}")
        print(f"Avg loss: {avg_loss:,.2f}")
        print(f"PF:       {pf:.2f}" if not np.isnan(pf) else "PF:       n/a")

        print("\nFirst few trades:")
        for t in trades[:10]:
            print(
                f"{t.entry_time} -> {t.exit_time} | "
                f"qty={t.qty:.4g} | {t.entry_px:.2f} -> {t.exit_px:.2f} | "
                f"pnl={t.pnl:,.2f} ({t.ret_pct:+.2f}%)"
            )
    else:
        print("No trades executed.")

    # Optional: write trades to CSV for further analysis
    if trade_csv is not None:
        trade_csv.parent.mkdir(parents=True, exist_ok=True)
        trade_df = pd.DataFrame(
            [
                {
                    "symbol": t.symbol,
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "entry_px": t.entry_px,
                    "exit_px": t.exit_px,
                    "qty": t.qty,
                    "pnl": t.pnl,
                    "ret_pct": t.ret_pct,
                }
                for t in trades
            ]
        )
        trade_df.to_csv(trade_csv, index=False)
        print(f"\nWrote {len(trades)} trades to {trade_csv}")


# -----------------------------
# CLI
# -----------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Offline CSV replay microlot tool.")
    p.add_argument("--csv", required=True, type=Path, help="Path to OHLCV CSV file.")
    p.add_argument("--symbol", required=True, help="Symbol name (for reporting).")
    p.add_argument("--cash", type=float, default=1000.0, help="Starting cash.")
    p.add_argument("--fast", type=int, default=5, help="Fast MA length.")
    p.add_argument("--slow", type=int, default=20, help="Slow MA length.")
    p.add_argument("--qty", type=float, default=1.0, help="Trade quantity per entry.")
    p.add_argument("--trade-csv", type=Path, default=None, help="Optional path to write trades CSV.")
    p.add_argument(
        "--tz",
        type=str,
        default=None,
        help="Optional timezone name (e.g. 'America/Chicago') for display only.",
    )
    return p.parse_args()


def main():
    args = parse_args()
    run_replay(
        csv_path=args.csv,
        symbol=args.symbol,
        cash=args.cash,
        fast=args.fast,
        slow=args.slow,
        trade_qty=args.qty,
        trade_csv=args.trade_csv,
        tz=args.tz,
    )


if __name__ == "__main__":
    main()
