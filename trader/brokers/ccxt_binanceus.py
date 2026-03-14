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


class BracketOrphanError(Exception):
    """
    Raised when a market entry succeeded but bracket placement failed AND
    the emergency close also failed.  The position is open on Binance.US
    with no protective orders — the caller must lock _pos and alert the user.
    """
    pass


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
        self._bracket_task: Optional[asyncio.Task] = None

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

        # 1) current price via REST (no persistent subscription)
        ticker = await self.exchange.fetch_ticker(self.contract)
        price = float(ticker['last'])

        # 2) base currency amount (qty is USD notional)
        amount = qty / price
        min_amount = self.exchange.markets[self.contract]['limits']['amount']['min'] or 0.0
        if amount < min_amount:
            amount = min_amount
        amount = self.exchange.amount_to_precision(self.contract, amount)

        # 2b) USDT balance check — scale down if needed, skip only if truly too small
        quote_currency = self.contract.split('/')[1]  # e.g. "USDT" from "LTC/USDT"
        bal = await self.exchange.fetch_balance()
        usdt_free = float(bal.get(quote_currency, {}).get('free', 0))
        cost_estimate = float(amount) * price
        MIN_ORDER_USD = 5.0  # hard floor — not worth trading below this
        if usdt_free < MIN_ORDER_USD:
            raise RuntimeError(
                f"[BINANCEUS] {self.contract}: {quote_currency} balance too low to trade "
                f"({usdt_free:.2f} available, minimum ${MIN_ORDER_USD}) — skipping order"
            )
        if usdt_free < cost_estimate * 1.01:
            # Not enough for the full allocation — scale down to what's available
            scaled_notional = usdt_free * 0.98  # 2% buffer for fees
            amount = scaled_notional / price
            amount = self.exchange.amount_to_precision(self.contract, amount)
            print(f"[BINANCEUS] {self.contract}: scaling order to {usdt_free:.2f} {quote_currency} "
                  f"(full allocation was ~{cost_estimate:.2f})")

        # 3) create orders: [market, stop_loss, target]
        trades = []
        entry_order = await self.exchange.create_order(self.contract, 'market', side.lower(), amount)
        trades.append(self._to_fake_ib_trade(entry_order))

        # 3b) wait 1s for settlement so coins are available for stop/target sell orders
        await asyncio.sleep(1)

        # stop + target — if either fails, emergency-close the entry position
        close_side = 'sell' if side.lower() == 'buy' else 'buy'
        try:
            # stop loss limit (Binance.US spot does not support stop_loss market orders)
            stop_limit = self.exchange.price_to_precision(
                self.contract, stop_price * (0.995 if close_side == 'sell' else 1.005)
            )
            stop_order = await self.exchange.create_order(
                self.contract, 'stop_loss_limit', close_side, amount, stop_limit,
                params={'stopPrice': self.exchange.price_to_precision(self.contract, stop_price)}
            )
            trades.append(self._to_fake_ib_trade(stop_order))

            # target limit
            target_order = await self.exchange.create_order(self.contract, 'limit', close_side,
                                                            amount, target_price)
            trades.append(self._to_fake_ib_trade(target_order))
        except Exception as e:
            print(f"[BINANCEUS] bracket placement failed after entry: {e} — emergency closing position")
            try:
                await self.exchange.create_order(self.contract, 'market', close_side, amount)
                print(f"[BINANCEUS] emergency close sent for {self.contract}")
            except Exception as e2:
                # Entry is open, bracket failed, emergency close failed — caller must lock the lot
                raise BracketOrphanError(
                    f"entry placed but bracket+emergency_close both failed: {e2}"
                ) from e
            raise

        return trades

    async def place_bracket_limit(self, side: str, qty: int, limit_price: float,
                                  stop: float, target: float, *,
                                  tif: Optional[str] = None,
                                  outsideRth: Optional[bool] = None) -> List[Trade]:
        """Same as above but parent is a limit order."""
        if self.contract is None:
            raise RuntimeError("Contract not resolved")

        ticker = await self.exchange.fetch_ticker(self.contract)
        price = float(ticker['last'])

        amount = qty / price
        min_amount = self.exchange.markets[self.contract]['limits']['amount']['min'] or 0.0
        if amount < min_amount:
            amount = min_amount
        amount = self.exchange.amount_to_precision(self.contract, amount)

        trades = []
        order = await self.exchange.create_order(self.contract, 'limit', side.lower(), amount, limit_price)
        trades.append(self._to_fake_ib_trade(order))

        stop_side = 'sell' if side.lower() == 'buy' else 'buy'
        stop_limit = self.exchange.price_to_precision(
            self.contract, stop * (0.995 if stop_side == 'sell' else 1.005)
        )
        stop_order = await self.exchange.create_order(
            self.contract, 'stop_loss_limit', stop_side, amount, stop_limit,
            params={'stopPrice': self.exchange.price_to_precision(self.contract, stop)}
        )
        trades.append(self._to_fake_ib_trade(stop_order))

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

        # 1) historical back-fill (retry on timeout — 4 symbols hit API simultaneously)
        since = self.exchange.milliseconds() - preload_days * 24 * 60 * 60 * 1000
        ohlcv = []
        for attempt in range(5):
            try:
                ohlcv = await self.exchange.fetch_ohlcv(self.contract, timeframe='1m', since=since, limit=prefill_n)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == 4:
                    print(f"[BINANCEUS] {self.contract} backfill failed after 5 attempts: {e} — continuing with empty history")
                    break
                wait = 10 * (attempt + 1)
                print(f"[BINANCEUS] {self.contract} backfill retry {attempt+1}/5 in {wait}s: {e}")
                await asyncio.sleep(wait)
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
        # stored but not used in microlot path (microlot uses watch_bracket instead)
        self._exec_handler = handler

    # ------------------------------------------------------------------
    # Bracket order monitor (OCA equivalent for spot)
    # ------------------------------------------------------------------
    def watch_bracket(self, stop_id: str, target_id: str, on_fill=None):
        """
        Start a background task that polls stop/target orders every 30s.
        When one fills, cancels the other and calls on_fill(filled_id).
        """
        if self._bracket_task and not self._bracket_task.done():
            self._bracket_task.cancel()
        self._bracket_task = asyncio.create_task(
            self._bracket_poll(stop_id, target_id, on_fill)
        )

    async def _bracket_poll(self, stop_id: str, target_id: str, on_fill=None):
        while True:
            await asyncio.sleep(30)
            try:
                s = await self.exchange.fetch_order(stop_id, self.contract)
                t = await self.exchange.fetch_order(target_id, self.contract)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[BINANCEUS] bracket poll error: {e}")
                continue

            if s.get('status') in ('closed', 'filled'):
                try:
                    await self.exchange.cancel_order(target_id, self.contract)
                except Exception as e:
                    print(f"[BINANCEUS] cancel target {target_id}: {e}")
                print(f"[BINANCEUS] {self.contract} STOP filled — target cancelled")
                if on_fill:
                    on_fill(stop_id)
                break
            elif t.get('status') in ('closed', 'filled'):
                try:
                    await self.exchange.cancel_order(stop_id, self.contract)
                except Exception as e:
                    print(f"[BINANCEUS] cancel stop {stop_id}: {e}")
                print(f"[BINANCEUS] {self.contract} TARGET filled — stop cancelled")
                if on_fill:
                    on_fill(target_id)
                break

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