"""
IBKR cash-quantity (fractional-share) broker.
Exposes the *same* public API as IbkrBroker so run_live.py and backtest
do not need to change.
"""
import asyncio
import inspect
from datetime import datetime, time, timedelta
from typing import Optional, List, Callable
import time as pytime

from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder, Trade
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

    def __init__(self, host: str, port: int, client_id: int, account: str = "", market_data_only: bool = False):
        self.ib = IB()
        self.host, self.port, self.client_id = host, port, client_id
        self.account = account or None
        self.contract: Optional[Stock] = None
        self._md_type: int = 1          # 1=live, 3=delayed
        self.use_rtb_for_equities = False   # match flag you already set
        self.market_data_only = market_data_only
        self.ib.RequestTimeout = 30
    # ------------------------------------------------------------------
    # Connection helpers (same names as IbkrBroker)
    # ------------------------------------------------------------------
    async def connect(self, *, readonly: bool = False):
        if self.ib.isConnected():
            return

        # --- MD-only connect path: avoid ib_insync connectAsync post-sync calls ---
        if self.market_data_only:
            await self.ib.client.connectAsync(self.host, self.port, clientId=self.client_id, timeout=15)
            # mark IB as connected (ib_insync uses this internal state)
            self.ib._logger.info(f"[IBKR] connected (market_data_only) clientId={self.client_id}")
            return

        # --- Normal trading connect path ---
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=15, readonly=readonly)
        await self._init_account_state()

    async def _init_account_state(self):
        """Resolve account ID from TWS and cache it."""
        if not self.account:
            managed = self.ib.managedAccounts()
            if managed:
                self.account = managed[0]
        print(f"[IBKR] connected, account={self.account}")

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
    async def resolve_contract(
        self, 
        symbol: str, 
        exchange: str = "SMART",
        currency: str = "USD", 
        *_, 
        **__,
    ) -> Stock:
        sym = symbol
        currency = currency or "USD"

        # For ETFs, try specific venues first
        exchange_candidates = []
        if sym in {"TQQQ", "SQQQ"}:
            exchange_candidates = ["NASDAQ", "SMART", "ARCA"]
        elif sym in {"SOXL", "SOXS"}:
            exchange_candidates = ["ARCA", "SMART", "NASDAQ"]
        else:
            exchange_candidates = [exchange or "SMART", "SMART", "ARCA", "NASDAQ"]

        last_err = None

        for ex in exchange_candidates:
            try:
                cds = await self.ib.reqContractDetailsAsync(Stock(sym, ex, currency))
                if not cds:
                    continue

                c = cds[0].contract

                # Probe: can we get *any* history fast?
                try:
                    bars = await asyncio.wait_for(
                        self.ib.reqHistoricalDataAsync(
                            c,
                            endDateTime="",
                            durationStr="1 D",
                            barSizeSetting="1 min",
                            whatToShow="TRADES",
                            useRTH=False,
                            formatDate=2,
                            keepUpToDate=False,
                        ),
                        timeout=12,
                    )
                except Exception as e:
                    bars = []
                    last_err = e

                if bars:
                    self.contract = c
                    print(f"[CONTRACT] {sym} exch={ex} conId={c.conId} (probe bars={len(bars)})", flush=True)
                    return self.contract

                print(f"[CONTRACT][NOHIST] {sym} exch={ex} conId={c.conId} probe_failed={type(last_err).__name__ if last_err else 'none'}", flush=True)

            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"IB could not qualify stock {sym} (last_err={last_err})")




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
                               prefill_n: int = 2000,
                               build_only: bool = False,
                               run_minutes: int = 0):
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
        what_candidates = [what_to_show] if what_to_show else []
        for w in ["TRADES", "MIDPOINT", "BID_ASK"]:
            if w not in what_candidates:
                what_candidates.append(w)

        sym = getattr(self.contract, "symbol", "?")
        conId = getattr(self.contract, "conId", None)
        
        if build_only:
            all_bars =[]
            end_dt = ""
            days_left = int(preload_days)

            chosen_what = None
            last_err = None
            while days_left > 0:
                got_chunk = False

                candidates = [chosen_what] if chosen_what else what_candidates

                for w in candidates:
                    try:
                        print(
                            f"[HIST][CHUNK][REQ] sym={sym} conId={conId} end={end_dt or 'NOW'} dur=1D what={w}",
                            flush=True
                        )
                        chunk = await asyncio.wait_for(
                            self.ib.reqHistoricalDataAsync(
                                self.contract,
                                endDateTime=end_dt,
                                durationStr="1 D",
                                barSizeSetting="1 min",
                                whatToShow=w,
                                useRTH=False,
                                formatDate=2,
                                keepUpToDate=False,  # IMPORTANT for build_only
                            ),
                            timeout=25,
                        )
                        if not chunk:
                            print(f"[HIST][CHUNK][EMPTY] sym={sym} what={w}", flush=True)
                            continue

                        chosen_what = w
                        all_bars.extend(chunk)
                        got_chunk = True

                        # move end_dt backwards to just before the oldest bar in this chunk
                        oldest = chunk[0].date
                        dt_oldest = dt_to_datetime(oldest) if not isinstance(oldest, datetime) else oldest
                        if dt_oldest.tzinfo is None:
                            dt_oldest = dt_oldest.replace(tzinfo=timezone.utc)
                        else:
                            dt_oldest = dt_oldest.astimezone(timezone.utc)

                        prev_dt = dt_oldest - timedelta(minutes=1)
                        end_dt = prev_dt.strftime("%Y%m%d %H:%M:%S") + " UTC"  # suffix tells IB to treat as UTC, not TWS local (ET)

                        days_left -= 1
                        print(
                            f"[HIST][CHUNK][GOT] sym={sym} what={w} bars={len(chunk)} days_left={days_left}",
                            flush=True
                        )

                        # pacing to avoid IB throttling/timeouts
                        await asyncio.sleep(1.0)

                        break  # done with what_candidates for this day

                    except asyncio.TimeoutError as e:
                        last_err = e
                        print(f"[HIST][CHUNK][TIMEOUT] sym={sym} what={w}", flush=True)
                    except Exception as e:
                        last_err = e
                        print(f"[HIST][CHUNK][ERR] sym={sym} what={w} {type(e).__name__}: {e}", flush=True)

                    # If we already found a working what earlier, don't keep cycling candidates forever
                if not got_chunk:
                    print(f"[HIST][CHUNK][STOP] sym={sym} could not fetch more history last_err={type(last_err).__name__ if last_err else ''}", flush=True)
                    break

            bars = all_bars
            if not bars:
                print(f"[HIST][WARN] {sym} got 0 bars after chunking.", flush=True)

        else:
            ku = True
            bars = []
            last_err = None
            for attempt in (1, 2):
                for w in what_candidates:
                    try:
                        bars = await asyncio.wait_for(
                            self.ib.reqHistoricalDataAsync(
                                self.contract,
                                endDateTime="",
                                durationStr=f"{preload_days} D",
                                barSizeSetting="1 min",
                                whatToShow=w,
                                useRTH=False,
                                formatDate=2,
                                keepUpToDate=ku
                            ),
                            timeout=25
                        )

                        if bars:
                            print(f"[HIST][GOT] sym={sym} what={w} bars_len={len(bars)}", flush=True)
                            break
                        else:
                            print(f"[HIST][EMPTY] sym={sym} what={w}", flush=True)

                    except asyncio.TimeoutError as e:
                        last_err = e
                        print(f"[HIST][ERR] sym={sym} what={w} {type(e).__name__}: {e}", flush=True)
                        continue
                    except Exception as e:
                        last_err = e
                        print(f"[HIST][ERR] sym={sym} what={w} attempt={attempt} {type(e).__name__}: {e}", flush=True)
                        continue
                if bars:
                    break
            if not bars:
                print(f"[HIST][WARN] {sym} got 0 bars after fallbacks. last_err={last_err}", flush=True)
            

        last_seen = None
        # Emit initial history (build_only emits all bars; live mode caps at prefill_n)
        emit_bars = bars if build_only else bars[-prefill_n:]
        n_emit = len(emit_bars)
        print(f"[HIST] about to emit n={n_emit} for {sym}")
        for b in emit_bars:
            d = _bar_to_dict(b)
            last_seen = d["datetime"]
            await _emit(d)
        
        print(f"[HIST] emitted n={n_emit} for {sym}")
        print(f"[RTB] emitted history bars: {n_emit}")
        if build_only:
            print(f"[BUILD_ONLY] history emitted for {sym} -> returning", flush=True)
            return



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
        print(f"[RTB] emitted history bars: {min(len(bars), prefill_n)}")




        bars.updateEvent += on_live_update
        print(f"[STREAM] subscribed updateEvent for {sym} (keepUpToDate=True)")
        print(f"[STREAM] entering live loop for {sym}")
        run_minutes = int(run_minutes or 0)  # if you have it as arg; otherwise remove
        stop_at = pytime.monotonic() + (run_minutes * 60) if run_minutes > 0 else None

        try:
            n = 0
            while True:
                await asyncio.sleep(1)
                n += 1
                if stop_at is not None and pytime.monotonic() >= stop_at:
                    print(f"[STREAM] stopping {sym} (run_minutes={run_minutes})", flush=True)
                    break
                if n % 10 == 0:
                    # show last_seen so we know if anything moved
                    print(f"[STREAM] alive {sym} last_seen={last_seen}")
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