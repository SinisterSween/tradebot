import asyncio, inspect
from contextlib import suppress
from dataclasses import dataclass
from typing import Optional, Callable, List
from ib_insync import IB, Contract, ContFuture, Future, util, Order, Trade, Stock
from datetime import datetime, timezone, timedelta
from ib_insync.order import LimitOrder, StopOrder, MarketOrder
from ib_insync import Order, RealTimeBar

util.patchAsyncio()

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
        self._md_type = md_type
        name = {1: "REALTIME", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}.get(md_type, str(md_type))
        print(f"[IBKR] requested market data type: {name}")
    from ib_insync import LimitOrder, StopOrder

    def _md_type_name(self, md: int) -> str:
        return {1: "REALTIME", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}.get(md, str(md))

    def enforce_market_data_guard(self, *, use_delayed: bool, allow_mismatch: bool = False):
        """
        Compare desired (from config) vs actual (from IB/TWS) market data type.
        Warn or abort based on allow_mismatch.
        """
        desired = 3 if use_delayed else 1
        # ib.client.marketDataType: 1=live, 2=frozen, 3=delayed, 4=delayed_frozen; 0 if unset
        actual_raw = getattr(getattr(self.ib, "client", None), "marketDataType", 0) or 0
        try:
            actual = int(actual_raw)
        except Exception:
            actual = 0
        if actual == 0:
            actual = desired  # fall back so we don't false-alarm on first connect tick

        print(f"[IBKR] market data: requested={self._md_type_name(desired)} actual={self._md_type_name(actual)}")

        if use_delayed and actual in (1, 2):
            msg = "Config wants DELAYED but IB is LIVE/FROZEN."
            if allow_mismatch:
                print(f"[WARN] {msg} Proceeding due to allow_data_mismatch=True.")
            else:
                raise RuntimeError(msg)

        elif not use_delayed and actual in (3, 4):
            msg = "Config wants LIVE but IB is DELAYED/DELAYED_FROZEN."
            if allow_mismatch:
                print(f"[WARN] {msg} Proceeding due to allow_data_mismatch=True.")
            else:
                raise RuntimeError(msg)



    def _apply_exec_flags(self, order, tif=None, outsideRth=None):
        if tif is not None:
            order.tif = str(tif)
        if outsideRth is not None:
            order.outsideRth = bool(outsideRth)
        return order


    async def whatif_bracket_limit(self, side: str, qty: int, limit_price: float, 
                                   stop: float, target: float, *,
                                   tif: str | None = None, outsideRth: bool | None = None):
        """
        Build a LIMIT parent + LIMIT TP + STOP SL as what-if (no placement).
        Returns dict with commission/margin estimates if available.
        """
        assert getattr(self, "contract", None) is not None, "IbkrBroker.contract not set"
        action = "BUY" if side.upper() == "BUY" else "SELL"
        exit_action = "SELL" if action == "BUY" else "BUY"

        parent = LimitOrder(action, qty, float(limit_price), tif="GTC")
        parent.whatIf = True
        take = LimitOrder(exit_action, qty, float(target), tif="GTC"); take.whatIf = True
        stop_o = StopOrder(exit_action, qty, float(stop), tif="GTC"); stop_o.whatIf = True

        self._apply_exec_flags(parent, tif, outsideRth); parent.whatIf = True
        self._apply_exec_flags(take,   tif, outsideRth); take.whatIf   = True
        self._apply_exec_flags(stop_o, tif, outsideRth); stop_o.whatIf = True

        # whatIf calls are sync in most ib_insync versions; patchAsyncio makes this fine
        p = self.ib.whatIfOrder(self.contract, parent)
        t = self.ib.whatIfOrder(self.contract, take)
        s = self.ib.whatIfOrder(self.contract, stop_o)

        def _summ(o):
            if not o: return {}
            # commission and margin fields differ by TWS version; normalize best-effort
            return {
                "initMarginChange": float(getattr(o, "initMarginChange", 0.0) or 0.0),
                "maintMarginChange": float(getattr(o, "maintMarginChange", 0.0) or 0.0),
                "equityWithLoanChange": float(getattr(o, "equityWithLoanChange", 0.0) or 0.0),
                "commission": float(getattr(o, "commission", 0.0) or 0.0),
                "minCommission": float(getattr(o, "minCommission", 0.0) or 0.0),
                "maxCommission": float(getattr(o, "maxCommission", 0.0) or 0.0),
                "warningText": getattr(o, "warningText", "") or "",
            }

        return {"parent": _summ(p), "take": _summ(t), "stop": _summ(s)}


    async def place_bracket_limit(self, side: str, qty: int, limit_price: float,
                                stop: float, target: float, *,
                              tif: str | None = None, outsideRth: bool | None = None):
        """
        Parent LIMIT + child LIMIT target + child STOP.
        Returns [parentTrade, takeProfitTrade, stopLossTrade].
        Requires self.contract to be set.
        """
        assert getattr(self, "contract", None) is not None, "IbkrBroker.contract not set"
        action = "BUY" if side.upper() == "BUY" else "SELL"
        exit_action = "SELL" if action == "BUY" else "BUY"

        parent = LimitOrder(action, qty, float(limit_price), tif=tif, outsideRth=outsideRth)
        parent.transmit = False

        take = LimitOrder(exit_action, qty, float(target), tif=tif, outsideRth=outsideRth)
        take.transmit = False

        stop_o = StopOrder(exit_action, qty, float(stop), tif=tif, outsideRth=outsideRth)
        # last child transmits the chain by default

        self._apply_exec_flags(parent, tif, outsideRth)
        self._apply_exec_flags(take,   tif, outsideRth)
        self._apply_exec_flags(stop_o, tif, outsideRth)


        pTrade = self.ib.placeOrder(self.contract, parent)
        #await self.ib.waitOnUpdate()                 # get parent orderId
        pid = pTrade.order.orderId

        take.parentId = pid
        stop_o.parentId = pid

        tTrade = self.ib.placeOrder(self.contract, take)
        sTrade = self.ib.placeOrder(self.contract, stop_o)

        return [pTrade, tTrade, sTrade]

    async def connect(self, *, readonly: bool = False):
        """Connect to TWS/IBG. Use readonly=True for what-if/data-only flows."""
        if not self.ib.isConnected():
            try:
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=15, readonly=readonly)
            except Exception as e:
                print(f"API connection failed: {e}")
                print("Hint: TWS > Global Configuration > API > Settings: enable API, and make sure Socket Port matches.")
                raise


    async def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()


    async def resolve_contract(self, symbol: str, exchange: str, currency: str, use_continuous: bool, front_month: str | None = None,
                           con_id: int | None = None, local_symbol: str | None = None):
        """
        Robust resolver for ES/MES **and** equities (SPY/QQQ).
        Priority:
        1) conId
        2) localSymbol (try Stock first, then FUT)
        3) explicit month (futures)
        4) front month discovery (futures)
        Sets self.contract and returns it.
        """
        exch = (exchange or "CME")   # we'll override to SMART in the equity path
        ccy  = (currency or "USD")

        # 1) conId (fastest, most reliable)
        if con_id:
            cds = await self.ib.reqContractDetailsAsync(Contract(conId=int(con_id)))
            if not cds:
                raise RuntimeError(f"Could not qualify conId={con_id}")
            self.contract = cds[0].contract
            return self.contract

        # 2) localSymbol (could be equity like "SPY" or future like "MESU5")
        if local_symbol:
            # Try equity first
            try:
                cds = await self.ib.reqContractDetailsAsync(Stock(local_symbol, exchange=exchange or "SMART", currency=ccy))
                if cds:
                    self.contract = cds[0].contract
                    return self.contract
            except Exception:
                pass
            # Try futures (your original logic)
            tmpl = Contract(secType="FUT", localSymbol=local_symbol, exchange=exch, currency=ccy)
            cds = await self.ib.reqContractDetailsAsync(tmpl)
            if not cds:
                for ex2 in ("GLOBEX", ""):
                    tmpl = Contract(secType="FUT", localSymbol=local_symbol, exchange=ex2 or None, currency=ccy)
                    cds = await self.ib.reqContractDetailsAsync(tmpl)
                    if cds:
                        break
            if not cds:
                raise RuntimeError(f"Could not qualify localSymbol={local_symbol}")
            self.contract = cds[0].contract
            return self.contract

        # === EQUITY PATH (SMART/NYSE/NASDAQ/etc.) ===
        eq_exchanges = {"SMART","NYSE","NASDAQ","ARCA","ISLAND","IEX","BATS","EDGX"}
        if (exchange or "").upper() in eq_exchanges:
            cds = await self.ib.reqContractDetailsAsync(Stock(symbol, exchange=exchange or "SMART", currency=ccy))
            if not cds:
                raise RuntimeError(f"No stock contract returned for {symbol}@{exchange or 'SMART'}")
            self.contract = cds[0].contract
            return self.contract

        # 3) explicit month
        if front_month and front_month != "front":
            tmpl = Future(symbol=symbol, exchange=exch, currency=ccy, lastTradeDateOrContractMonth=str(front_month))
            cds = await self.ib.reqContractDetailsAsync(tmpl)
            if not cds:
                # retry with GLOBEX / unspecified
                for ex2 in ("GLOBEX", ""):
                    tmpl = Future(symbol=symbol, exchange=ex2 or None, currency=ccy,
                                lastTradeDateOrContractMonth=str(front_month))
                    cds = await self.ib.reqContractDetailsAsync(tmpl)
                    if cds:
                        break
            if not cds:
                raise RuntimeError(f"IB could not qualify explicit {symbol} ({front_month}).")
            self.contract = cds[0].contract
            return self.contract

        # 4) 'front' month discovery
        # list all months and pick nearest non-expired
        for ex2 in ("CME", "GLOBEX", ""):
            cds = await self.ib.reqContractDetailsAsync(Future(symbol=symbol, exchange=ex2 or None, currency=ccy))
            if cds:
                break
        if not cds:
            raise RuntimeError(f"No futures returned for {symbol} on any exchange.")

        now = datetime.now(timezone.utc)

        def expiry_dt(s: str):
            if len(s) == 6:
                y, m = int(s[:4]), int(s[4:6])
                return datetime(y, m, 1, tzinfo=timezone.utc) + timedelta(days=35)
            y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
            return datetime(y, m, d, tzinfo=timezone.utc)

        candidates = [cd for cd in cds if cd.contract.lastTradeDateOrContractMonth]
        future = min(
            [cd for cd in candidates if expiry_dt(cd.contract.lastTradeDateOrContractMonth) > now + timedelta(days=2)] or candidates,
            key=lambda cd: expiry_dt(cd.contract.lastTradeDateOrContractMonth)
        )
        self.contract = future.contract
        return self.contract

    
    async def stream_realtime_bars(
        self, *, on_bar, what_to_show="TRADES", bar_size_secs=60, preload_days=2, prefill_n=2000):
        """
        Historical + keepUpToDate feed that works in both delayed and real-time.
        Prefills last `prefill_n` bars into `on_bar(...)`, then pushes live updates.
        Keeps this coroutine alive so callers can cancel it cleanly.
        """
        

        assert getattr(self, "contract", None) is not None, "IbkrBroker.contract not set"

        def _bar_to_dict(b):
            # b.date can be str or datetime depending on formatDate
            dt = util.parseIBDatetime(b.date) if isinstance(getattr(b, "date", None), str) else getattr(b, "date", None)
            if dt is None:
                dt = getattr(b, "time", None)
            if hasattr(dt, "tzinfo") and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.isoformat().replace("+00:00", "Z") if hasattr(dt, "isoformat") else str(dt)
            return {
                "datetime": ts,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(getattr(b, "volume", 0.0) or 0.0),
            }
        async def _emit(d):
            try:
                if inspect.iscoroutinefunction(on_bar):
                    await on_bar(d)
                else:
                    on_bar(d)
            except Exception as e:
                print(f"[STREAM] on_bar error: {e}")

        c = self.contract
        md_actual = getattr(self, "_md_type", 0)
        # ---------- EQUITIES LIVE BRANCH (place THIS block BEFORE your hist/keepUpToDate code) ----------
        if getattr(c, "secType", "") == "STK" and md_actual in (1, 2):
            assert int(bar_size_secs) == 60, "Equity RT aggregator assumes 60s bars"
            print("[STREAM] equities real-time path engaged (5s -> 60s)")

            # 1) backfill recent 1-min bars WITHOUT keepUpToDate
            bars = await self.ib.reqHistoricalDataAsync(
                c, endDateTime="", durationStr="2 D",
                barSizeSetting="1 min", whatToShow=what_to_show,
                useRTH=True, formatDate=2, keepUpToDate=False
            )
            # emit backfill
            for b in bars[-int(prefill_n or 2000):]:
                await _emit(_bar_to_dict(b))
            print(f"[STREAM] initial bars loaded: {len(bars)} (equity backfill)")

            # 2) subscribe to true real-time 5s bars and aggregate to 60s
            rtb = self.ib.reqRealTimeBars(c, barSize=5, whatToShow=what_to_show, useRTH=True)

            bucket = None; o = h = l = cl = None; vol = 0

            def on_rtb(b: RealTimeBar):
                nonlocal bucket, o, h, l, cl, vol
                ts = util.dt_to_datetime(b.time).replace(tzinfo=timezone.utc)
                bkt = ts.replace(second=0, microsecond=0)
                px = float(b.close); v = int(b.volume or 0)

                if bucket is None:
                    bucket = bkt; o = h = l = cl = px; vol = v; return

                if bkt == bucket:
                    cl = px; vol += v; h = max(h, px); l = min(l, px)
                else:
                    asyncio.create_task(_emit({
                        "datetime": bucket.isoformat(), 
                        "open": o, "high": h, "low": l, "close": cl, "volume": vol
                    }))
                    bucket = bkt; o = h = l = cl = px; vol = v

            rtb.updateEvent += on_rtb
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass  # allows cancellation
            finally:
                rtb.updateEvent -= on_rtb
                self.ib.cancelRealTimeBars(rtb)

            return 

        

        # Historical series that keeps itself up to date
        bars = self.ib.reqHistoricalData(
            self.contract,
            endDateTime="",
            durationStr=f"{int(preload_days)} D",
            barSizeSetting=("1 min" if int(bar_size_secs) == 60 else f"{int(bar_size_secs)} secs"),
            whatToShow=what_to_show,
            useRTH=False,
            formatDate=2,       # deliver datetimes
            keepUpToDate=True,
        )

        # ---- Prefill RollingBars with historical buffer ----
        try:
            n_total = len(bars)
            if n_total:
                start = max(0, n_total - int(prefill_n))
                seed = list(bars)[start:]  # include last bar; your live gates handle staleness
                for b in seed:
                    await _emit(_bar_to_dict(b))
            print(f"[STREAM] initial bars loaded: {n_total}")
        except Exception as e:
            print(f"[STREAM] prefill error: {e}")

        # ---- Live updates via updateEvent ----
        q = asyncio.Queue()
        last_iso = None

        def _on_update(_bars, hasNewBar):
            if not _bars:
                return
            try:
                q.put_nowait(_bars[-1])  # last bar (new or updated)
            except Exception:
                pass

        try:
            bars.updateEvent += _on_update
        except Exception as e:
            print(f"[STREAM] failed to attach updateEvent: {e}")

        pump_task = None
        try:
            async def _pump():
                nonlocal last_iso
                while True:
                    b = await q.get()
                    d = _bar_to_dict(b)
                    if d["datetime"] != last_iso:
                        await _emit(d)
                        last_iso = d["datetime"]

            pump_task = asyncio.create_task(_pump())

            # keep this coroutine alive so callers can cancel it (shutdown path)
            while True:
                await asyncio.sleep(5)
        finally:
            with suppress(Exception):
                bars.updateEvent -= _on_update
            if pump_task:
                pump_task.cancel()
                with suppress(asyncio.CancelledError):
                    await pump_task




    
    async def place_bracket_market(self, side: str, qty: int, stop_price: float, target_price: float, *,
                                   tif: str | None = None, outsideRth: bool | None = None):
        if self.contract is None:
            raise RuntimeError("Contract not set.")
        action = "BUY" if side.upper() == "BUY" else "SELL"
        parent = Order(action=action, totalQuantity=qty, orderType="MKT", transmit=False)
        stop = Order(action=("SELL" if action == "BUY" else "BUY"), totalQuantity=qty, orderType="STP",
                     auxPrice=float(stop_price), parentId=0, transmit=False)
        target = Order(action=("SELL" if action == "BUY" else "BUY"), totalQuantity=qty, orderType="LMT",
                       lmtPrice=float(target_price), parentId=0, transmit=True)
        trades = self.ib.bracketOrder(self.contract, parent, takeProfitOrder=target, stopLossOrder=stop)

        self._apply_exec_flags(parent, tif, outsideRth)
        self._apply_exec_flags(target,   tif, outsideRth)
        self._apply_exec_flags(stop, tif, outsideRth)


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

    def _pick_account(self, account_hint: str | None) -> str:
        if account_hint:
            return account_hint
        accts = self.ib.managedAccounts()
        if not accts:
            raise RuntimeError("No managed accounts available from IB.")
        return accts[0]


    
    async def start_pnl_stream(self, account: str | None, conId: int | None, handler):
        acct = account or (self.ib.managedAccounts()[0] if self.ib.managedAccounts() else None)
        if not acct:
            raise RuntimeError("No managed accounts available from IB.")

        if getattr(self, "_pnl_task", None):
            self._pnl_task.cancel()
            self._pnl_task = None

        def _emit(realized, unreal):
            try:
                if inspect.iscoroutinefunction(handler):
                    asyncio.create_task(handler(realized, unreal))
                else:
                    handler(realized, unreal)
            except Exception as e:
                print(f"[PNL-POLL] handler error: {e}")

        async def _poll():
            while True:
                try:
                    summary = self.ib.accountSummary()
                    realized = unreal = 0.0
                    for v in summary:
                        if v.account == acct:
                            if v.tag == "RealizedPnL":
                                try: realized = float(v.value or 0.0)
                                except: pass
                            elif v.tag == "UnrealizedPnL":
                                try: unreal = float(v.value or 0.0)
                                except: pass
                    _emit(realized, unreal)
                except Exception as e:
                    print(f"[PNL-POLL] error: {e}")
                await asyncio.sleep(5.0)

        self._pnl_task = asyncio.create_task(_poll())
        return None

    async def stop_pnl_stream(self):
        # Cancel polling task (fallback mode)
        task = getattr(self, "_pnl_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._pnl_task = None

        # Cancel event-based subs (if ever used)
        try:
            if getattr(self, "_pnl_obj", None):
                self.ib.cancelPnL(self._pnl_obj)
                self._pnl_obj = None
            if getattr(self, "_pnl_single", None):
                self.ib.cancelPnLSingle(self._pnl_single)
                self._pnl_single = None
        except Exception:
            pass
    
    def is_connected(self) -> bool:
        return bool(getattr(self.ib, "isConnected", lambda: False)())

    async def ensure_connected(self):
        if self.is_connected():
            return
        try:
            await self.connect()
            # reapply data mode
            if getattr(self, "_md_type", None) is not None:
                self.ib.reqMarketDataType(self._md_type)
        except Exception as e:
            print(f"[IB] reconnect failed: {e}")

    
