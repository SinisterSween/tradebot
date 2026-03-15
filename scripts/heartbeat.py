#!/usr/bin/env python3
"""
Heartbeat and daily-summary Discord notifications for the trading bot.

  - Heartbeat:      posts a live-status ping every HEARTBEAT_INTERVAL_H hours
  - Daily summary:  posts a day-end recap when midnight UTC rolls over and resets counters

Wire into run_portfolio:
    from scripts.heartbeat import heartbeat_loop
    ht_task = asyncio.create_task(heartbeat_loop(lot_status, stop_event))

lot_status schema (dict keyed by lot name, maintained by run_microlot):
    {
        "CRYPTO-ETH": {
            "trades_today":  0,    # entries fired today (live or paper)
            "targets_today": 0,    # TARGET exits today
            "stops_today":   0,    # STOP/FORCE exits today
            "pnl_today":     0.0,  # approximate P&L in USD (R-based estimate)
            "open":          False,
            "open_symbol":   "",
            "open_side":     "",
        },
        ...
    }
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Dict

HEARTBEAT_INTERVAL_H = 4   # hours between regular pings


# ── helpers ──────────────────────────────────────────────────────────────────

def _notify(msg: str) -> None:
    """Send msg to Discord/Slack — silently swallows all errors."""
    try:
        import scripts.notify_slack as _ns
        _ns.notify(msg)
    except Exception:
        pass


def _pnl_str(p: float) -> str:
    return f"`{'+' if p >= 0 else ''}{p:.2f}`"


def _format_heartbeat(now: datetime, lot_status: Dict) -> str:
    lines = [f"💓 **Bot alive** — {now.strftime('%Y-%m-%d %H:%M UTC')}"]
    total_trades = 0
    total_pnl    = 0.0

    for lot_name, st in lot_status.items():
        t  = st.get("trades_today", 0)
        p  = st.get("pnl_today",   0.0)
        total_trades += t
        total_pnl    += p
        pos_str = (
            f"  🔵 {st.get('open_symbol', '')} {st.get('open_side', '')}"
            if st.get("open") else ""
        )
        lines.append(
            f"`{lot_name}`:  {t} trade{'s' if t != 1 else ''}  {_pnl_str(p)}{pos_str}"
        )

    arrow = "📈" if total_pnl >= 0 else "📉"
    lines.append(
        f"{arrow} **Day total**: {total_trades} trades  {_pnl_str(total_pnl)}"
    )
    return "\n".join(lines)


def _format_daily_summary(date_str: str, lot_status: Dict) -> str:
    lines = [f"📊 **Daily Summary — {date_str}**"]
    total_trades = 0
    total_pnl    = 0.0

    for lot_name, st in lot_status.items():
        t       = st.get("trades_today",  0)
        p       = st.get("pnl_today",    0.0)
        targets = st.get("targets_today", 0)
        stops   = st.get("stops_today",   0)
        total_trades += t
        total_pnl    += p
        if t > 0:
            pnl_part    = f"  {_pnl_str(p)}"
            detail_part = f"  ✅ {targets}  ❌ {stops}"
        else:
            pnl_part    = ""
            detail_part = "  —"
        lines.append(
            f"`{lot_name}`:  {t} trade{'s' if t != 1 else ''}{pnl_part}{detail_part}"
        )

    arrow = "📈" if total_pnl >= 0 else "📉"
    lines.append(
        f"{arrow} **Total**: {total_trades} trades  {_pnl_str(total_pnl)}"
    )
    return "\n".join(lines)


# ── main coroutine ────────────────────────────────────────────────────────────

async def heartbeat_loop(lot_status: Dict, stop_event: asyncio.Event) -> None:
    """
    Background asyncio task.  Wakes every 60 s to check:
      1. Has the calendar date rolled over?  → send daily summary + reset counters
      2. Has HEARTBEAT_INTERVAL_H elapsed?   → send live-status heartbeat
    """
    interval_secs = HEARTBEAT_INTERVAL_H * 3600

    # Post the first heartbeat ~2 min after startup (let brokers finish connecting)
    last_heartbeat_mono = time.monotonic() - interval_secs + 120
    last_summary_date   = datetime.now(timezone.utc).date()

    while not stop_event.is_set():
        try:
            await asyncio.sleep(60)       # check every minute
        except asyncio.CancelledError:
            break

        if stop_event.is_set():
            break

        now   = datetime.now(timezone.utc)
        today = now.date()

        # ── Daily summary: fires once when the UTC date flips ─────────────────
        if today != last_summary_date:
            msg = _format_daily_summary(last_summary_date.isoformat(), lot_status)
            print(f"[HEARTBEAT] daily summary for {last_summary_date}")
            _notify(msg)
            # Reset all daily counters for the new day
            for v in lot_status.values():
                v["trades_today"]  = 0
                v["pnl_today"]     = 0.0
                v["targets_today"] = 0
                v["stops_today"]   = 0
            last_summary_date   = today
            last_heartbeat_mono = time.monotonic()  # avoid double-post

        # ── Heartbeat: fires every HEARTBEAT_INTERVAL_H hours ─────────────────
        if time.monotonic() - last_heartbeat_mono >= interval_secs:
            msg = _format_heartbeat(now, lot_status)
            print(f"[HEARTBEAT] ping at {now.strftime('%H:%M UTC')}")
            _notify(msg)
            last_heartbeat_mono = time.monotonic()
