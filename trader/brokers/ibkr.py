import asyncio
from dataclasses import dataclass
from typing import Optional, Callable, List
from ib_insync import IB, Contract, ContFuture, Future, util, Order, Trade

@dataclass
class BracketPrices:
    entry_mkt: bool = True
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None

class IbkrBroker:
    def __init__(self, host: str, port: int, client_id: int, account: str = ""):
        self.ib = IB()
        self.host, self.port, self.client_id = host, port, client_id
        self.account = account or None
        self.contract: Optional[Contract] = None

    def set_market_data_type(self, md_type: int):
        # 1=REALTIME, 2=FROZEN, 3=DELAYED, 4=DELAYED_FROZEN
        self.ib.reqMarketDataType(md_type)

    async def connect(self):
        if not self.ib.isConnected():
            try:
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=15, readonly=True)
            except Exception as e:
                print(f"API connection failed: {e}")
                print("Hint: TWS > Global Configuration > API > Settings: enable API, and make sure Socket Port matches.")
                raise

    async def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    async def resolve_contract(self, symbol: str, exchange: str, currency: str,
                               use_continuous: bool, front_month: str | None):
        if use_continuous:
            c = ContFuture(symbol=symbol, exchange=exchange)
        else:
            if not front_month:
                raise ValueError("front_month is required when use_continuous is False (eg '202509').")
            c = Future(symbol=symbol, lastTradeDateOrContractMonth=front_month,
                       exchange=exchange, currency=currency)
        qs = await self.ib.qualifyContractsAsync(c)
        if not qs:
            raise RuntimeError(
                f"IB could not qualify contract for {symbol} "
                f"(continuous={use_continuous}, month={front_month}) on {exchange}/{currency}."
            )
        self.contract = qs[0]
        return self.contract

    async def stream_realtime_bars(self, on_bar, what_to_show: str = "TRADES", bar_size_secs: int = 60):
        if self.contract is None:
            raise RuntimeError("Contract not set.")
        bar_size = "1 min" if bar_size_secs >= 60 else "5 secs"
        duration = "1800 S" # seed window
        
        bars = self.ib.reqHistoricalData(
            self.contract,
            endDateTime='',
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=True,
            keepUpToDate=True,
            formatDate=1
        )

        def on_update(blist, hasNewBar):
            if not blist: return
            b = blist[-1]
            on_bar({
                "datetime": b.date,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume,
            })
        bars.updateEvent += on_update
        try:
            while True:
                await asyncio.sleep(1.0)
        finally:
            try:
                bars.updateEvent -= on_update
                bars.cancel()
            except Exception:
                pass

    async def place_bracket_market(self, side: str, qty: int, stop_price: float, target_price: float) -> List[Trade]:
        if self.contract is None:
            raise RuntimeError("Contract not set.")
        action = "BUY" if side.upper() == "BUY" else "SELL"
        parent = Order(action=action, totalQuantity=qty, orderType="MKT", transmit=False)
        stop = Order(action=("SELL" if action == "BUY" else "BUY"), totalQuantity=qty, orderType="STP",
                     auxPrice=float(stop_price), parentId=0, transmit=False)
        target = Order(action=("SELL" if action == "BUY" else "BUY"), totalQuantity=qty, orderType="LMT",
                       lmtPrice=float(target_price), parentId=0, transmit=True)
        trades = self.ib.bracketOrder(self.contract, parent, takeProfitOrder=target, stopLossOrder=stop)
        return trades

    async def flatten_all(self):
        positions = await self.ib.reqPositionsAsync()
        for p in positions:
            if self.account and p.account != self.account:
                continue
            qty = p.position
            if qty == 0:
                continue
            action = "SELL" if qty > 0 else "BUY"
            o = Order(action=action, orderType="MKT", totalQuantity=abs(qty), tif="DAY", transmit=True)
            await self.ib.placeOrderAsync(p.contract, o)

    def on_exec_details(self, handler):
        self.ib.execDetailsEvent += handler

    async def start_pnl_stream(self, account: str | None, contract_conId: int | None, handler):
        if account is None:
            return
        if contract_conId:
            pnl = self.ib.pnlSingle(account, "", contract_conId)
            pnl.updateEvent += lambda p: handler(p.realizedPnL or 0.0, p.unrealizedPnL or 0.0)
        else:
            pnla = self.ib.pnl(account)
            pnla.updateEvent += lambda p: handler(p.dailyPnL or 0.0, p.unrealizedPnL or 0.0)
