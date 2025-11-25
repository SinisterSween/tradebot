"""
Micro-lot crypto broker via CCXT (Binance.US).
Imitates the *same* public API as IbkrBroker so run_live.py needs zero changes.
"""
import asyncio
import inspect
from datetime import datetime, timezone
from typing import Optional, List, Callable

import ccxt.pro as ccxt  # async ccxt
from ib_insync import Trade, Order, MarketOrder, LimitOrder, StopOrder


class CcxtBinanceus:
    def __init__(self, api_key: str, secret: str, sandbox: bool = False):
        self.exchange = ccxt.binanceus({
            'apiKey': api_key,
            'secret': secret,
            'sandbox': sandbox,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'},
        })
        self.symbols: List[str] = []          # set later
        self.contract: Optional[str] = None   # "BTC/USDC"
        self._balances = {}

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------
    async def connect(self, *, readonly: bool = False):
        await self.exchange.load_markets()
        print("[BINANCEUS] markets loaded")

    async def disconnect(self):
        await self.exchange.close()

    def is_connected(self) -> bool:
        return self.exchange is not None

    async def ensure_connected(self):
        if not self.is_connected():
            await self.connect()

    # ------------------------------------------------------------------
    # Market data (no-op guards to satisfy caller)
    # ------------------------------------------------------------------
    def set_market_data_type(self, md_type: int):
        pass

    def enforce_market_data_guard(self, *, use_delayed: bool, allow_mismatch: bool = False):
        pass

    # ------------------------------------------------------------------
    # Contract resolution (crypto pairs)
    # ------------------------------------------------------------------
    async def resolve_contract(self, symbol: str, *_, **__) -> str:
        """
        symbol: "BTC/USDC"
        returns the same string so rest of code can treat it like a contract
        """
        if symbol not in self.exchange.symbols:
            raise RuntimeError(f"Binance.US does not list {symbol}")
        self.contract = symbol
        return symbol

    # ------------------------------------------------------------------
    # Order entry (micro lots)
    # ------------------------------------------------------------------
    async def place_bracket_market(self, side: str, qty: int,
                                   stop_price: float, target_price: float, *,
                                   tif: Optional[str] = None,
                                   outsideRth: Optional[bool] = None) -> List[Trade]:
        """
        Market parent + STOP loss + LIMIT target.
        qty is in *quote currency units* (e.g. 10 USDC worth of BTC).
        We convert to base-currency amount via current price.
        """
        if self.contract is None:
            raise RuntimeError("Contract not resolved")

        # 1) current price
        ticker = await self.exchange.watch_ticker(self.contract)
        price = float(ticker['last'])
        await self.exchange.stop_ticker(self.contract)  # stop the watch stream

        # 2) base currency amount
        amount = qty / price
        min_amount = self.exchange.markets[self.contract]['limits']['amount']['min']
        if amount < min_amount:
            amount = min_amount  # floor to exchange minimum
        amount = self.exchange.amount_to_precision(self.contract, amount)

        # 3) create orders
        trades = []
        # parent market
        params = {}
        order = await self.exchange.create_order(self.contract, 'market', side.lower(), amount, params=params)
        parent_trade = self._to_fake_ib_trade(order)
        trades.append(parent_trade)

        # stop loss
        stop_side = 'sell' if side.lower() == 'buy' else 'buy'
        stop_order = await self.exchange.create_order(self.contract, 'stop_loss_limit', stop_side,
                                                      amount, stop_price, params={'stopPrice': stop_price})
        trades.append(self._to_fake_ib_trade(stop_order))

        # target
        target_side = 'sell' if side.lower() == 'buy' else 'buy'
        target_order = await self.exchange.create_order(self.contract, 'limit', target_side,
                                                        amount, target_price)
        trades.append(self._to_fake_ib_trade(target_order))
        return trades

    async def place_bracket_limit(self, side: str, qty: int, limit_price: float,
                                  stop: float, target: float, *,
                                  tif: Optional[str] = None,
                                  outsideRth: Optional[bool] = None) -> List[Trade]:
        """Same as above but parent is a limit order."""
        if self.contract is None:
            raise RuntimeError("Contract not resolved")

        ticker = await self.exchange.watch_ticker(self.contract)
        price = float(ticker['last'])
        await self.exchange.stop_ticker(self.contract)

        amount = qty / price
        min_amount = self.exchange.markets[self.contract]['limits']['amount']['min']
        if amount < min_amount:
            amount = min_amount
        amount = self.exchange.amount_to_precision(self.contract, amount)

        trades = []
        # parent limit
        params = {}
        order = await self.exchange.create_order(self.contract, 'limit', side.lower(), amount,
                                                 limit_price, params=params)
        trades.append(self._to_fake_ib_trade(order))

        # stop
        stop_side = 'sell' if side.lower() == 'buy' else 'buy'
        stop_order = await self.exchange.create_order(self.contract, 'stop_loss_limit', stop_side,
                                                      amount, stop, params={'stopPrice': stop})
        trades.append(self._to_fake_ib_trade(stop_order))

        # target
        target_side = 'sell' if side.lower() == 'buy' else 'buy'
        target_order = await self.exchange.create_order(self.contract, 'limit', target_side,
                                                        amount, target)
        trades.append(self._to_fake_ib_trade(target_order))
        return trades

    # ------------------------------------------------------------------
    # Stream 1-min bars (CCXT OHLCV → your bar dict)
    # ------------------------------------------------------------------
    async def stream_realtime_bars(self, *, on_bar: Callable,
                                   what_to_show: str = "TRADES",
                                   bar_size_secs: int = 60,
                                   preload_days: int = 2,
                                   prefill_n: int = 2000):
        """
        Watch 1-min OHLCV stream and push bar dicts identical to IBKR path.
        """
        import inspect

        if self.contract is None:
            raise RuntimeError("Contract not resolved")

        async def _emit(d):
            if inspect.iscoroutinefunction(on_bar):
                await on_bar(d)
            else:
                on_bar(d)

        # 1) historical back-fill
        since = self.exchange.milliseconds() - preload_days * 24 * 60 * 60 * 1000
        ohlcv = await self.exchange.fetch_ohlcv(self.contract, timeframe='1m', since=since, limit=prefill_n)
        for row in ohlcv[-prefill_n:]:
            ts, o, h, l, c, v = row
            await _emit({
                "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            })

        # 2) live 1-min stream
        while True:
            try:
                ohlcv = await self.exchange.watch_ohlcv(self.contract, timeframe='1m')
                for row in ohlcv:
                    ts, o, h, l, c, v = row
                    await _emit({
                        "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                        "open": float(o),
                        "high": float(h),
                        "low": float(l),
                        "close": float(c),
                        "volume": float(v),
                    })
            except Exception as e:
                print(f"[BINANCEUS] stream error: {e}")
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Helpers you already call
    # ------------------------------------------------------------------
    async def flatten_all(self):
        positions = await self.exchange.fetch_positions()
        for pos in positions:
            if float(pos['contracts']) == 0:
                continue
            side = 'sell' if float(pos['contracts']) > 0 else 'buy'
            symbol = pos['symbol']
            amount = abs(float(pos['contracts']))
            await self.exchange.create_order(symbol, 'market', side, amount)

    def on_exec_details(self, handler: Callable):
        # CCXT has no unified execDetails event; we ignore for now
        pass

    async def start_pnl_stream(self, account: Optional[str], conId: Optional[int], handler: Callable):
        # polling version
        async def _poll():
            while True:
                try:
                    bal = await self.exchange.fetch_balance()
                    realized = float(bal.get('info', {}).get('totalRealizedPnL', 0))
                    unrealized = float(bal.get('info', {}).get('totalUnrealizedPnL', 0))
                    if inspect.iscoroutinefunction(handler):
                        await handler(realized, unrealized)
                    else:
                        handler(realized, unrealized)
                except Exception as e:
                    print(f"[BINANCEUS] pnl poll error: {e}")
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
    # Private: convert CCXT order → fake ib_insync.Trade so caller
    # can still access trade.order.orderId etc.
    # ------------------------------------------------------------------
    def _to_fake_ib_trade(self, ccxt_order):
        """
        Returns an object that quacks like ib_insync.Trade
        (only the tiny subset you actually touch in run_live.py).
        """
        class FakeOrder:
            def __init__(self, d):
                self.orderId = d['id']

        class FakeTrade:
            def __init__(self, d):
                self.order = FakeOrder(d)

        return FakeTrade(ccxt_order)