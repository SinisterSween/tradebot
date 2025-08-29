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
    async def connect(self):
        if not self.ib.connected():
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=15)
    async def disconnect(self):
        if self.ib.connected(): self.ib.disconnect()
    async def resolve_contract(self, symbol: str, exchange: str, currency: str, use_continuous: bool = True, front_month: str = "") -> Contract:
        c = ContFuture(symbol, exchange=exchange) if use_continuous else Future(symbol=symbol, lastTradeDateOrContractMonth=front_month, exchange=exchange, currency=currency)
        q = await self.ib.qualifyContractsAsync(c)
        if not q: raise RuntimeError("Failed to qualify contract.")
        self.contract = q[0]; return self.contract
    async def stream_realtime_bars(self, on_bar: Callable, what_to_show: str = "TRADES", bar_size_secs: int = 60):
        if self.contract is None: raise RuntimeError("Contract not set.")
        rtb = self.ib.reqRealTimeBars(self.contract, whatToShow=what_to_show, useRTH=True, barSize=5)
        agg_open = agg_high = agg_low = agg_close = None; agg_vol = 0; last_bucket = None
        async for bar in rtb:
            ts = util.datetimeToNaive(bar.time)
            bucket = int(ts.timestamp() // bar_size_secs)
            if last_bucket is None: last_bucket = bucket
            if bucket != last_bucket:
                if agg_open is not None:
                    on_bar({"datetime": ts.replace(second=0, microsecond=0), "open": agg_open, "high": agg_high, "low": agg_low, "close": agg_close, "volume": agg_vol})
                agg_open = agg_high = agg_low = agg_close = None; agg_vol = 0; last_bucket = bucket
            if agg_open is None:
                agg_open, agg_high, agg_low = bar.open, bar.high, bar.low
            else:
                agg_high = max(agg_high, bar.high); agg_low = min(agg_low, bar.low)
            agg_close = bar.close; agg_vol += bar.volume
    async def place_bracket_market(self, side: str, qty: int, stop_price: float, target_price: float) -> List[Trade]:
        if self.contract is None: raise RuntimeError("Contract not set.")
        action = "BUY" if side.upper() == "BUY" else "SELL"
        parent = Order(action=action, totalQuantity=qty, orderType="MKT", transmit=False)
        stop = Order(action=("SELL" if action == "BUY" else "BUY"), totalQuantity=qty, orderType="STP", auxPrice=float(stop_price), parentId=0, transmit=False)
        target = Order(action=("SELL" if action == "BUY" else "BUY"), totalQuantity=qty, orderType="LMT", lmtPrice=float(target_price), parentId=0, transmit=True)
        trades = self.ib.bracketOrder(self.contract, parent, takeProfitOrder=target, stopLossOrder=stop)
        return trades
    async def flatten_all(self):
        positions = await self.ib.positionsAsync()
        for p in positions:
            if self.account and p.account != self.account: continue
            qty = p.position
            if qty == 0: continue
            action = "SELL" if qty > 0 else "BUY"
            o = Order(action=action, orderType="MKT", totalQuantity=abs(qty), tif="DAY", transmit=True)
            await self.ib.placeOrderAsync(p.contract, o)
    def on_exec_details(self, handler):
        def _h(trade, fill): handler(trade, fill)
        self.ib.execDetailsEvent += _h
    async def start_pnl_stream(self, account: str | None, contract_conId: int | None, handler):
        if account is None: return
        if contract_conId:
            pnl = self.ib.pnlSingle(account, "", contract_conId)
            pnl.updateEvent += lambda p: handler(p.realizedPnL or 0.0, p.unrealizedPnL or 0.0)
        else:
            pnla = self.ib.pnl(account)
            pnla.updateEvent += lambda p: handler(p.dailyPnL or 0.0, p.unrealizedPnL or 0.0)
