"""
IBKR cash-quantity (fractional-share) broker.
Exposes the *same* public API as IbkrBroker so run_live.py and backtest
do not need to change.
"""
import asyncio
import inspect
from datetime import datetime, timezone
from typing import Optional, List, Callable

from ib_insync import IB, Stock, Order, MarketOrder, LimitOrder, StopOrder, Trade
from ib_insync.util import parseIBDatetime as dt_to_datetime

# Re-use the *exact* data-class you already import in ibkr.py
from trader.brokers.ibkr import BracketPrices   # brings in your dataclass

pending = set()
class IbkrFractional:
    """
    Thin wrapper around ib_insync.IB that:
    - uses Stock (not Future) contracts
    - sends cashQty orders for fractional shares
    - returns ib_insync.Trade objects so the rest of your code is untouched
    """

    def __init__(self, host: str, port: int, client_id: int, account: str = ""):
        self.ib = IB()
        self.host, self.port, self.client_id = host, port, client_id
        self.account = account or None
        self.contract: Optional[Stock] = None
        self._md_type: int = 1          # 1=live, 3=delayed
        self.use_rtb_for_equities = False   # match flag you already set

    # ------------------------------------------------------------------
    # Connection helpers (same names as IbkrBroker)
    # ------------------------------------------------------------------
    async def connect(self, *, readonly: bool = False):
        if not self.ib.isConnected():
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id,
                                       timeout=15, readonly=readonly)

    async def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    async def ensure_connected(self):
        if not self.is_connected():
            await self.connect()

    # ------------------------------------------------------------------
    # Market-data helpers (identical interface)
    # ------------------------------------------------------------------
    def set_market_data_type(self, md_type: int):
        self.ib.reqMarketDataType(md_type)
        self._md_type = md_type

    def enforce_market_data_guard(self, *, use_delayed: bool, allow_mismatch: bool = False):
        # Same logic you already use; simplified here
        desired = 3 if use_delayed else 1
        actual  = getattr(self.ib.client, "marketDataType", desired) or desired
        if desired != actual and not allow_mismatch:
            raise RuntimeError(f"Data mismatch: wanted {desired}, IB gives {actual}")

    # ------------------------------------------------------------------
    # Contract resolution (stocks only)
    # ------------------------------------------------------------------
    async def resolve_contract(self, symbol: str, exchange: str = "SMART",
                               currency: str = "USD", *_, **__) -> Stock:
        """
        Resolve a stock contract and store it.
        Extra positional/keyword args accepted so caller can keep futures args
        without crashing.
        """
        cds = await self.ib.reqContractDetailsAsync(
            Stock(symbol, exchange or "SMART", currency or "USD")
        )
        if not cds:
            raise RuntimeError(f"IB could not qualify stock {symbol}")
        self.contract = cds[0].contract
        return self.contract

    # ------------------------------------------------------------------
    # Order entry (cash-quantity style)
    # ------------------------------------------------------------------
    async def place_bracket_market(self, side: str, qty: int,
                                   stop_price: float, target_price: float, *,
                                   tif: Optional[str] = None,
                                   outsideRth: Optional[bool] = None) -> List[Trade]:
        """
        Market parent + STOP loss + LIMIT target.
        qty is ignored for *shares*; we derive dollar amount from
        account equity × risk pct and send cashQty instead.
        """
        if self.contract is None:
            raise RuntimeError("Contract not resolved")

        dollar_amount = self._dollar_risk_to_cash_qty(side, qty, stop_price)
        action = "BUY" if side.upper() == "BUY" else "SELL"
        exit_action = "SELL" if action == "BUY" else "BUY"

        # Parent market order (cashQty)
        parent = MarketOrder(action, totalQuantity=0)
        parent.cashQty = round(dollar_amount, 2)
        parent.tif = tif or "DAY"
        parent.outsideRth = outsideRth or False
        parent.transmit = False

        # Stop loss
        stop = StopOrder(exit_action, totalQuantity=0,
                         stopPrice=round(stop_price, 2))
        stop.cashQty = round(dollar_amount, 2)
        stop.parentId = 0
        stop.tif = tif or "DAY"
        stop.outsideRth = outsideRth or False
        stop.transmit = False

        # Target
        target = LimitOrder(exit_action, totalQuantity=0,
                            lmtPrice=round(target_price, 2))
        target.cashQty = round(dollar_amount, 2)
        target.parentId = 0
        target.tif = tif or "DAY"
        target.outsideRth = outsideRth or False
        target.transmit = True   # last child transmits the chain

        trades = self.ib.bracketOrder(self.contract, parent,
                                      takeProfitOrder=target,
                                      stopLossOrder=stop)
        return trades

    async def place_bracket_limit(self, side: str, qty: int, limit_price: float,
                                  stop: float, target: float, *,
                                  tif: Optional[str] = None,
                                  outsideRth: Optional[bool] = None) -> List[Trade]:
        """Same as above but parent is a limit order."""
        if self.contract is None:
            raise RuntimeError("Contract not resolved")

        dollar_amount = self._dollar_risk_to_cash_qty(side, qty, stop)
        action = "BUY" if side.upper() == "BUY" else "SELL"
        exit_action = "SELL" if action == "BUY" else "BUY"

        parent = LimitOrder(action, totalQuantity=0,
                            lmtPrice=round(limit_price, 2))
        parent.cashQty = round(dollar_amount, 2)
        parent.tif = tif or "DAY"
        parent.outsideRth = outsideRth or False
        parent.transmit = False

        take = LimitOrder(exit_action, totalQuantity=0,
                          lmtPrice=round(target, 2))
        take.cashQty = round(dollar_amount, 2)
        take.parentId = 0
        take.tif = tif or "DAY"
        take.outsideRth = outsideRth or False
        take.transmit = False

        stop_o = StopOrder(exit_action, totalQuantity=0,
                           stopPrice=round(stop, 2))
        stop_o.cashQty = round(dollar_amount, 2)
        stop_o.parentId = 0
        stop_o.tif = tif or "DAY"
        stop_o.outsideRth = outsideRth or False
        stop_o.transmit = True

        trades = self.ib.bracketOrder(self.contract, parent,
                                      takeProfitOrder=take,
                                      stopLossOrder=stop_o)
        return trades

    # ------------------------------------------------------------------
    # Streamer (copy-paste of your futures logic, but contract=Stock)
    # ------------------------------------------------------------------
    async def stream_realtime_bars(self, *, on_bar,
                               what_to_show: str = "TRADES",
                               bar_size_secs: int = 60,
                               preload_days: int = 2,
                               prefill_n: int = 2000):
        """
        1-min bars using IB historical with keepUpToDate=True.
        Works with delayed data; avoids RealTimeBars which often doesn't.
        """
        import asyncio, inspect
        from datetime import timezone

        if self.contract is None:
            raise RuntimeError("Contract not resolved")
        
        async def _emit(d):
            try:
                if inspect.iscoroutinefunction(on_bar):
                    await on_bar(d)
                else:
                    on_bar(d)
            except Exception as e:
                print(f"[RTB][EMIT-ERROR] {type(e).__name__}: {e}")
                raise



        def _to_utc_iso(x):
            # IB sometimes gives BarData.date as datetime, sometimes as string
            if isinstance(x, datetime):
                dt = x
            else:
                dt = dt_to_datetime(x)   # parseIBDatetime
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat()


        def _bar_to_dict(b):
            raw = getattr(b, "date", None)
            return {
                "datetime": _to_utc_iso(raw),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(getattr(b, "volume", 0) or 0),
            }

        # Backfill + live updates via historical keepUpToDate
        bars = await self.ib.reqHistoricalDataAsync(
            self.contract,
            endDateTime="",
            durationStr=f"{preload_days} D",
            barSizeSetting="1 min",
            whatToShow=what_to_show,
            useRTH=False,              # IMPORTANT: allow outside RTH
            formatDate=2,
            keepUpToDate=True          # IMPORTANT: live updates
        )
        last_seen = None
        # Emit initial history
        n_emit = min(len(bars), prefill_n)
        print(f"[HIST] about to emit n={n_emit} for {getattr(self.contract,'symbol','?')}")
        for b in bars[-prefill_n:]:
            d = _bar_to_dict(b)
            last_seen = d["datetime"]
            await _emit(d)
        print(f"[HIST] emitted n={n_emit} for {getattr(self.contract,'symbol','?')}")
        print(f"[RTB] emitted history bars: {min(len(bars), prefill_n)}")


        last_dbg = {"ts": 0.0}

        def on_live_update(bars_, hasNewBar=None):
            nonlocal last_seen
            try:
                if not bars_:
                    return

                b = bars_[-1]
                d = _bar_to_dict(b)          # d["datetime"] is already UTC ISO string
                ts = d["datetime"]

                now = asyncio.get_event_loop().time()
                if now - last_dbg["ts"] > 30:
                    print(f"[RTB] updateEvent ok sym={getattr(self.contract,'symbol','?')} ts={ts}")
                    last_dbg["ts"] = now

                # dedupe
                if ts == last_seen:
                    return

                last_seen = ts
                asyncio.create_task(_emit(d))

            except Exception as e:
                print(f"[HIST][UPDATE][ERROR] {type(e).__name__}: {e}")



        bars.updateEvent += on_live_update
        print(f"[STREAM] subscribed updateEvent for {getattr(self.contract,'symbol','?')} (keepUpToDate=True)")
        print(f"[STREAM] entering live loop for {getattr(self.contract,'symbol','?')}")
        print(f"[TEST] about to emit history for {getattr(self.contract,'symbol','?')} bars={len(bars)}")

        try:
            n = 0
            while True:
                await asyncio.sleep(1)
                n += 1
                if n % 10 == 0:
                    # show last_seen so we know if anything moved
                    print(f"[STREAM] alive {getattr(self.contract,'symbol','?')} last_seen={last_seen}")
        except asyncio.CancelledError:
            pass
        finally:
            bars.updateEvent -= on_live_update
            try:
                self.ib.cancelHistoricalData(bars)
            except Exception:
                pass



    # ------------------------------------------------------------------
    # Helpers you already call
    # ------------------------------------------------------------------
    async def flatten_all(self):
        positions = await self.ib.reqPositionsAsync()
        for p in positions:
            if self.account and p.account != self.account:
                continue
            qty = p.position
            if qty == 0:
                continue
            action = "SELL" if qty > 0 else "BUY"
            order = MarketOrder(action, totalQuantity=abs(qty))
            order.cashQty = abs(qty) * p.avgCost   # dollar flatten
            await self.ib.placeOrderAsync(p.contract, order)

    def on_exec_details(self, handler: Callable):
        self.ib.execDetailsEvent += handler

    async def start_pnl_stream(self, account: Optional[str], conId: Optional[int], handler: Callable):
        # polling version (same as your ibkr.py)
        import asyncio
        async def _poll():
            while True:
                try:
                    summary = self.ib.accountSummary()
                    realized = unrealized = 0.0
                    for v in summary:
                        if v.account == (account or self.account):
                            if v.tag == "RealizedPnL":
                                realized = float(v.value or 0.0)
                            elif v.tag == "UnrealizedPnL":
                                unrealized = float(v.value or 0.0)
                    if inspect.iscoroutinefunction(handler):
                        await handler(realized, unrealized)
                    else:
                        handler(realized, unrealized)
                except Exception as e:
                    print(f"[PNL] poll error: {e}")
                await asyncio.sleep(5)
        self._pnl_task = asyncio.create_task(_poll())

    async def stop_pnl_stream(self):
        if hasattr(self, "_pnl_task"):
            self._pnl_task.cancel()
            try:
                await self._pnl_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Private: convert desired share-qty → dollar amount for cashQty
    # ------------------------------------------------------------------
    def _dollar_risk_to_cash_qty(self, side: str, qty: int, stop_price: float) -> float:
        """
        Very small helper: derive dollar amount from risk logic.
        Override this if you want fancier sizing.
        """
        # For now we simply risk $50 per trade regardless of qty
        # (you can wire your RiskGovernor here later)
        return 50.0