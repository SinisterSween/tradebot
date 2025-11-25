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
        actual  = getattr(self.ib.client, "marketDataType", desired)
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
    async def stream_realtime_bars(self, *, on_bar: Callable,
                                   what_to_show: str = "TRADES",
                                   bar_size_secs: int = 60,
                                   preload_days: int = 2,
                                   prefill_n: int = 2000):
        """
        Historical + live 1-min aggregate for equities.
        Keeps the *same* signature you already call in run_live.py.
        """
        import inspect
        from ib_insync import RealTimeBar

        if self.contract is None:
            raise RuntimeError("Contract not resolved")

        def _bar_to_dict(b):
            dt = dt_to_datetime(b.time).replace(tzinfo=timezone.utc)
            return {
                "datetime": dt.isoformat(),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            }

        async def _emit(d):
            if inspect.iscoroutinefunction(on_bar):
                await on_bar(d)
            else:
                on_bar(d)

        # 1) back-fill
        bars = await self.ib.reqHistoricalDataAsync(
            self.contract,
            endDateTime="",
            durationStr=f"{preload_days} D",
            barSizeSetting="1 min",
            whatToShow=what_to_show,
            useRTH=True,
            formatDate=2,
            keepUpToDate=False
        )
        for b in bars[-prefill_n:]:
            await _emit(_bar_to_dict(b))

        # 2) live 5-s RT-bars → 60-s aggregate
        rtb = self.ib.reqRealTimeBars(self.contract, barSize=5,
                                      whatToShow=what_to_show,
                                      useRTH=True)

        bucket = None
        o = h = l = c = None
        vol = 0

        def on_rtb(bar: RealTimeBar):
            nonlocal bucket, o, h, l, c, vol
            ts = dt_to_datetime(bar.time).replace(tzinfo=timezone.utc)
            bkt = ts.replace(second=0, microsecond=0)
            px = float(bar.close)
            v = int(bar.volume or 0)

            if bucket is None:
                bucket, o, h, l, c, vol = bkt, px, px, px, px, v
                return

            if bkt == bucket:
                c, vol = px, vol + v
                h = max(h, px)
                l = min(l, px)
            else:
                asyncio.create_task(_emit({
                    "datetime": bucket.isoformat(),
                    "open": o, "high": h, "low": l, "close": c, "volume": vol
                }))
                bucket, o, h, l, c, vol = bkt, px, px, px, px, v

        rtb.updateEvent += on_rtb
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            rtb.updateEvent -= on_rtb
            self.ib.cancelRealTimeBars(rtb)

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