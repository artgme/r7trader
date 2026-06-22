"""
mozg.py — Live trading engine for r7trader.

Connects to a broker (ccxt or IBKR), feeds historical + live OHLCV bars into
a backtrader cerebro, runs the selected strategy, and routes any generated
signals to the real exchange as market orders.  Fills are posted as markers to
an optional Dash chart and appended to a CSV log.

Thread layout
─────────────
  main thread   – runs cerebro (blocking); processes strategy signals bar-by-bar
  data-worker   – fetches bars from the exchange, pushes them to the feed queue
"""

import csv
import logging
import queue
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

import backtrader as bt
import pandas as pd
import requests

logger = logging.getLogger(__name__)

# position state — the only valid values for Mozg.position
FLAT  = 'flat'
LONG  = 'long'
SHORT = 'short'


# Shortest safe poll window per timeframe: covers ~5 bars so timestamp comparison
# always has context even if one poll is delayed.
# IBKR duration units: S (seconds), D (days), W (weeks), M (months), Y (years).
_IBKR_POLL_DURATION: dict = {
    '1m':  '3600 S',  # 1 hour  (~60 bars; wider window avoids empty after-hours gaps)
    '5m':  '1800 S',  # 30 min  (~6 bars)
    '10m': '3600 S',  # 1 hour  (~6 bars)
    '15m': '7200 S',  # 2 hours (~8 bars)
    '30m': '1 D',
    '45m': '1 D',
    '1h':  '1 D',
    '2h':  '1 D',
    '4h':  '1 D',
    '1d':  '5 D',
}


# ─── Custom backtrader live feed ──────────────────────────────────────────────

class _QueueFeed(bt.feed.DataBase):
    """
    Backtrader DataBase feed backed by a thread-safe queue.
    Czyta dane z queue.Queue.
    Each item in the queue: (datetime, open, high, low, close, volume).
    Pushing None is the stop sentinel — _load() returns False and cerebro exits.
    """
    params = (
        ('queue', None),        # queue.Queue instance supplied by Mozg
        ('live_timeout', 5),    # seconds to block per _load() call before returning None
        ('on_go_live', None),   # callable invoked when 'GO_LIVE' sentinel is consumed
    )

    def islive(self):
        return True

    def haslivedata(self):
        return not self.p.queue.empty()

    #Backtrader uzywa _load() powtarzalnie aby wczytac kolejny bar
    def _load(self):
        try:
            bar = self.p.queue.get(timeout=self.p.live_timeout)
        except queue.Empty:#kolejna jest pusta (BT czeka i spróbuje ponownie później).
            return None  # no bar yet; cerebro will call _load again shortly
        if bar is None:
            return False  # stop sentinel; cerebro will shut down
        if bar == 'GO_LIVE':
            if self.p.on_go_live:
                self.p.on_go_live()
            return None  # no bar to load; cerebro retries
        dt, o, h, l, c, v = bar
        # backtrader stores datetimes as matplotlib ordinals; strip tzinfo for compat
        dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        self.lines.datetime[0]     = bt.date2num(dt_naive)
        self.lines.open[0]         = float(o)
        self.lines.high[0]         = float(h)
        self.lines.low[0]          = float(l)
        self.lines.close[0]        = float(c)
        self.lines.volume[0]       = float(v)
        self.lines.openinterest[0] = 0.0
        return True


# ─── Strategy wrapper ─────────────────────────────────────────────────────────

def _make_live_strategy(strategy_class, on_fill_cb):
    """
    Zmienia backtesting strategy na live-trading.
    Wiąze wewnętrznego brokera Cerebro z rzeczywistym brokerem.
    """
    class _Wrapped(strategy_class):
        def notify_order(self, order):
            super().notify_order(order) #super() - Python daje dostep do metody nadrzednej - wywyłuje notify order z klasy strategii, e.g. MomentumV8, RSIStrategy
            if order.status == order.Completed: #if order is complete, notify
                on_fill_cb(order.isbuy(), abs(order.executed.size), order.executed.price)

    _Wrapped.__name__ = strategy_class.__name__
    return _Wrapped


# ─── Mozg trading engine ──────────────────────────────────────────────────────

class Mozg:
    """
    Live trading engine.

    symbol          : exchange symbol — ccxt format 'SOL/USD', IBKR format 'AAPL'
    timeframe       : bar size in ccxt notation: '1m', '5m', '1h', …
    strategy_class  : uninstantiated bt.Strategy subclass from strategies/
    strategy_params : dict of kwargs forwarded verbatim to the strategy
    broker_type     : 'ccxt' | 'ibkr'
    trade_size      : fixed units per order (e.g. 1.0 = 1 SOL)
    history_limit   : historical bars preloaded to warm up indicators
    poll_interval_s : seconds between live polls (ccxt only)
    dash_url        : base URL of a running DashPlotter, e.g. 'http://127.0.0.1:8051'
    csv_path        : path to the trade log CSV
    starting_cash   : virtual cash for bt's internal paper broker (position tracking only)
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        strategy_class,
        strategy_params: dict = None,
        broker_type: str = 'ccxt',
        trade_size: float = 1.0,
        history_limit: int = 200,
        poll_interval_s: int = 10,
        dash_url: str = None,
        csv_path: str = 'trades.csv',
        starting_cash: float = 1_000_000.0,
        paper_mode: bool = True,
        order_type: str = 'market',   # 'market' | 'limit'
        ibkr_exchange: str = 'SMART',
        ibkr_currency: str = 'USD',
        ibkr_client_id: int = 80,
    ):
        self.symbol          = symbol
        self.timeframe       = timeframe
        self.strategy_class  = strategy_class
        self.strategy_params = strategy_params or {}
        self.broker_type     = broker_type
        # Bracket order params read from strategy_params; used by the IBKR entry path.
        # take_profit_price is mozg-level only and not forwarded to BT unless the strategy
        # explicitly declares it in its params tuple (e.g. RSIBracketStrategy).
        self._bracket_trail_pct   = self.strategy_params.get('trail_stop_pct')
        self._bracket_take_profit = self.strategy_params.get('take_profit_price')
        self.trade_size      = trade_size
        self.history_limit   = history_limit
        self.poll_interval_s = poll_interval_s
        self.dash_url        = dash_url
        self.csv_path        = csv_path
        self.starting_cash   = starting_cash
        self.paper_mode      = paper_mode   # True = log signals but never send real orders
        self.order_type      = order_type   # 'market' | 'limit' (limit uses last bar close)
        self.ibkr_exchange   = ibkr_exchange
        self.ibkr_currency   = ibkr_currency
        self.ibkr_client_id  = ibkr_client_id

        # public — readable from outside at any time
        self.position: str = FLAT   # 'flat' | 'long' | 'short'

        # private
        self._feed_queue:        queue.Queue   = queue.Queue()
        self._stop_event:        threading.Event = threading.Event()
        self._ibkr_order_queue:  queue.Queue   = queue.Queue()
        self._trader                            = None   # CexTrader or IBKRGateway
        self._ibkr_contract                     = None
        self._ibkr_connected:    threading.Event = threading.Event()
        self._live_trading:      bool            = False  # False during historical replay; True once GO_LIVE sentinel consumed
        self._entry_price: float | None         = None   # used to classify TP vs SL exits
        self._last_price:  float | None         = None

    # ── Connection ───────────────────────────────────────────────────────────────

    # Connect to the exchange and sync the starting position from the real broker.
    # Must be called before run().  Returns False if connection fails.
    def connect(self) -> bool:
        if self.broker_type == 'ccxt':
            return self._connect_ccxt()
        return self._connect_ibkr()

    def _connect_ccxt(self) -> bool:
        from cex import CexTrader
        self._trader = CexTrader()
        if not self._trader.connect():
            return False
        self.position = self._sync_position_ccxt()
        logger.info('Starting position: %s', self.position)
        return True

    def _connect_ibkr(self) -> bool:
        # IBKRGateway (and IB()) is created inside _ibkr_data_worker() after util.startLoop(),
        # so that ib_insync's async machinery runs on the worker thread's event loop.
        # Nothing to do here — run() waits on _ibkr_connected for the worker to confirm.
        return True

    # ── Position sync ────────────────────────────────────────────────────────────

    # For ccxt spot: if we hold >= 50% of trade_size of the base currency, assume long.
    def _sync_position_ccxt(self) -> str:
        balances = self._trader.get_balances()
        if not balances:
            return FLAT
        base = self.symbol.split('/')[0]
        held = float(balances.get(base, 0.0))
        if held >= self.trade_size * 0.9:  # 90% tolerance handles fees/rounding
            logger.info('Found %s balance: %.4f  →  LONG', base, held)
            return LONG
        return FLAT

    # For IBKR: scan open positions for this symbol and return the direction.
    def _sync_position_ibkr(self) -> str:
        for p in self._trader.get_positions():
            sym = getattr(p.contract, 'localSymbol', None) or p.contract.symbol
            if sym.upper() == self.symbol.upper():
                if p.position > 0:
                    logger.info('IBKR position %s: %.4f  →  LONG', sym, p.position)
                    return LONG
                elif p.position < 0:
                    logger.info('IBKR position %s: %.4f  →  SHORT', sym, p.position)
                    return SHORT
        return FLAT

    def _set_live_trading(self):
        self._live_trading = True
        logger.info('Historical replay complete — live trading enabled.')

    # ── Main entry point ─────────────────────────────────────────────────────────

    # Signal this engine to stop from any thread (used for multi-symbol shutdown).
    def stop(self):
        self._stop_event.set()
        self._feed_queue.put(None)

    # Build cerebro, start the data worker, and block until stopped.
    # Set handle_sigint=False when running multiple instances so the caller
    # manages Ctrl-C centrally instead of each instance fighting over the handler.
    def run(self, handle_sigint: bool = True):
        live_cls = _make_live_strategy(self.strategy_class, self._on_bt_fill)

        feed = _QueueFeed(queue=self._feed_queue, on_go_live=self._set_live_trading)

        cerebro = bt.Cerebro(runonce=False)
        cerebro.adddata(feed)
        cerebro.addstrategy(live_cls, **self.strategy_params)
        cerebro.addsizer(bt.sizers.FixedSize, stake=self.trade_size)
        cerebro.broker.setcash(self.starting_cash)

        self._init_csv()

        #wystartuj backgraound thread który pobiera dane z brokera i wrzuca do kolejki
        #metody zakupu znajdują się w _ibkr_data_worker
        target = self._ccxt_data_worker if self.broker_type == 'ccxt' else self._ibkr_data_worker #uzywac ccxt czy IBKR
        worker = threading.Thread(target=target, daemon=True, name='data-worker') #stworz background thread
        worker.start()

        if self.broker_type == 'ibkr':
            if not self._ibkr_connected.wait(timeout=30):
                logger.error('IBKR data worker did not connect within 30 s — aborting.')
                self.stop()
                return
            # position is now synced by the worker; apply it to cerebro
            if self.position == LONG:
                cerebro.broker.setposition(feed, size=self.trade_size, price=0.0)
            elif self.position == SHORT:
                cerebro.broker.setposition(feed, size=-self.trade_size, price=0.0)

        #umozliwia wyłwia zatrzymanie programu Ctrl-C (SIGINT) w terminalu
        if handle_sigint:
            def _sigint(*_):
                logger.info('Stopping…')
                self.stop()
            signal.signal(signal.SIGINT, _sigint)

        logger.info(
            'Mozg running  symbol=%s  timeframe=%s  strategy=%s  broker=%s  size=%s',
            self.symbol, self.timeframe, self.strategy_class.__name__,
            self.broker_type, self.trade_size,
        )
        #Runs cerebro, który uzywa next() na kazdym barze
        cerebro.run() #tutaj skrypt wykonuje się dopóki nie zostanie wywołana self.stop() lub Ctrl-C
        self._stop_event.set()
        worker.join(timeout=5)
        logger.info('Mozg stopped.  Final position: %s', self.position)

    # ── Data workers ─────────────────────────────────────────────────────────────

    # ccxt worker: preload historical bars then stream live bars into the feed queue.
    def _ccxt_data_worker(self):
        bars = self._trader.fetch_historical_ohlcv(
            self.symbol, self.timeframe, limit=self.history_limit
        )
        if bars:
            for ts_ms, o, h, l, c, v in bars:
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                self._feed_queue.put((dt, o, h, l, c, v))
            logger.info('Preloaded %d historical bars.', len(bars))
        self._feed_queue.put('GO_LIVE')

        def on_bar(bar):
            ts_ms, o, h, l, c, v = bar
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            self._last_price = float(c)
            self._feed_queue.put((dt, o, h, l, c, v))


        #pulling data every poll_interval_s seconds, and pushing to the queue
        self._trader.stream_live_ohlcv(
            self.symbol, self.timeframe,
            callback=on_bar,
            stop_event=self._stop_event,
            seed_bars=3,
            poll_interval_s=self.poll_interval_s,
        )

    # IBKR worker: preload historical bars, then poll every 60 s for new closed bars.
    # Polling sidesteps keepUpToDate=True callback delivery issues in Python 3.13
    # background threads (asyncio event-loop dispatch never fires updateEvent).
    def _ibkr_data_worker(self):
        import asyncio
        # eventkit (ib_insync dependency) calls get_event_loop() at import time,
        # so a loop must exist in this thread before the import happens.
        asyncio.set_event_loop(asyncio.new_event_loop())

        from ibkr import IBKRGateway
        self._trader = IBKRGateway(client_id=self.ibkr_client_id)
        if not self._trader.connect():
            logger.error('[mozg.py] IBKR data worker: connection failed.')
            self._ibkr_connected.set()  # unblock run() so it can abort cleanly
            return

        self._ibkr_contract = self._trader.make_stock_contract(
            self.symbol, exchange=self.ibkr_exchange, currency=self.ibkr_currency
        )
        self.position = self._sync_position_ibkr()
        logger.info('Starting position: %s', self.position)
        self._ibkr_connected.set()  # unblock run() — position is ready

        def on_portfolio_update(item):
            if getattr(item.contract, 'symbol', '') == self.symbol:
                logger.info('[%s] Portfolio: pos=%.0f price=%.4f unrealPNL=%.2f realPNL=%.2f',
                            self.symbol, item.position, item.marketPrice,
                            item.unrealizedPNL, item.realizedPNL)

        def on_position(_, contract, position, avgCost):
            if getattr(contract, 'symbol', '') == self.symbol:
                logger.info('[%s] Position: %.0f @ avgCost=%.4f', self.symbol, position, avgCost)

        self._trader.ib.updatePortfolioEvent += on_portfolio_update
        self._trader.ib.positionEvent += on_position

        hist = self._trader.fetch_historical(
            self._ibkr_contract, duration='2 D',
            bar_size=self.timeframe, what_to_show='TRADES',
        )
        if hist:
            for bar in hist[-self.history_limit:]:
                ts = pd.Timestamp(bar.date)
                ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
                dt = ts.to_pydatetime()
                self._feed_queue.put((dt, bar.open, bar.high, bar.low, bar.close,
                                      getattr(bar, 'volume', 0.0)))
            logger.info('Preloaded %d IBKR bars.', min(len(hist), self.history_limit))
        self._feed_queue.put('GO_LIVE')

        def _to_ts(date_val):
            ts = pd.Timestamp(date_val)
            return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')

        # Anchor: only bars strictly after this timestamp are treated as new closed bars.
        # If the initial preload failed (empty), anchor to now so we don't replay history.
        last_bar_ts = _to_ts(hist[-1].date) if hist else pd.Timestamp.now(tz='UTC')

        # Poll every 60 s — safe for IBKR's 60-requests-per-10-min pacing limit with
        # up to ~20 simultaneous strategies.  Each 1-second ib.sleep() tick also drives
        # the asyncio loop so that portfolio/position events continue to arrive.
        POLL_TICKS = 60
        tick = 0

        try:
            while not self._stop_event.is_set():
                # Drain the order queue (orders posted from the main/cerebro thread)
                try:
                    req = self._ibkr_order_queue.get_nowait()
                    if req.get('is_entry') and self._bracket_trail_pct is not None:
                        limit_price = self._last_price if self.order_type == 'limit' else None
                        self._trader.place_bracket_trailing(
                            self._ibkr_contract,
                            action=req['side'],
                            quantity=req['size'],
                            trail_percent=self._bracket_trail_pct,
                            limit_price=limit_price,
                            take_profit_price=self._bracket_take_profit,
                        )
                    else:
                        self._trader.place_market_order(self._ibkr_contract, req['side'], req['size'])
                    logger.info('IBKR ORDER  %s  size=%.4f', req['side'], req['size'])
                except queue.Empty:
                    pass

                self._trader.ib.sleep(1)
                tick += 1
                if tick < POLL_TICKS:
                    continue
                tick = 0

                # Fetch the latest bars and detect any that closed since last poll
                poll_duration = _IBKR_POLL_DURATION.get(self.timeframe, '1 D')
                fresh = self._trader.fetch_historical(
                    self._ibkr_contract, duration=poll_duration,
                    bar_size=self.timeframe, what_to_show='TRADES',
                )
                if not fresh:
                    continue

                self._last_price = float(fresh[-1].close)
                logger.info('%s : $%.2f', self.symbol, fresh[-1].close)

                for bar in fresh:
                    bar_ts = _to_ts(bar.date)
                    if bar_ts > last_bar_ts:
                        dt = bar_ts.to_pydatetime()
                        logger.info('%s [CLOSED] : $%.2f', self.symbol, bar.close)
                        self._feed_queue.put((dt, bar.open, bar.high, bar.low, bar.close,
                                              getattr(bar, 'volume', 0.0)))
                        last_bar_ts = bar_ts

        except ConnectionError:
            logger.error('[%s] IBKR Gateway disconnected — stopping engine.', self.symbol)
            self._stop_event.set()
            self._feed_queue.put(None)  # unblock cerebro so it shuts down cleanly

        self._trader.disconnect()

    # ── Order intercept ──────────────────────────────────────────────────────────

    # Called by the wrapped strategy each time bt's paper broker fills an order.
    # 1. Klasyfikuje zakupy
    # 2. Przekazuje do rzeczywistego brokera (chyba że paper_mode=True)
  
    def _on_bt_fill(self, is_buy: bool, size: float, price: float):
        self._last_price = price
        ep = self._entry_price   # entry price recorded when we opened the position

        if is_buy: #sygnał - LONG
            if self.position == FLAT:
                action, new_position = 'enter_long', LONG
                self._entry_price = price
            else:
                # closing a short: profit if fill price < entry price
                action       = 'exit_short_tp' if (ep is not None and price < ep) else 'exit_short_sl'
                new_position = FLAT
                self._entry_price = None
        else: #sygnał - SHORT
            if self.position == FLAT:
                action, new_position = 'enter_short', SHORT
                self._entry_price = price
            else:
                # closing a long: profit if fill price > entry price
                action       = 'exit_long_tp' if (ep is not None and price > ep) else 'exit_long_sl'
                new_position = FLAT
                self._entry_price = None

        self.position = new_position

        if not self._live_trading:
            return  # historical replay — position tracking only, no real orders

        logger.info(
            'SIGNAL  %-22s  price=%.4f  size=%.4f  position: %s → %s',
            action, price, size, self.position, new_position,
        )

        # route to real broker (skipped in paper mode)
        if self.paper_mode:
            logger.info('PAPER MODE — order not sent to exchange')
        elif self.broker_type == 'ccxt':
            self._execute_ccxt('buy' if is_buy else 'sell', size)
        else:
            self._ibkr_order_queue.put({
                'side': 'BUY' if is_buy else 'SELL',
                'size': size,
                'is_entry': action in ('enter_long', 'enter_short'),
            })

        self._send_dash_marker(action, price)
        self._log_trade(action, price, size)

    # ── Exchange execution ───────────────────────────────────────────────────────

    # Send a market order to the ccxt exchange; log the result or the error.
    def _execute_ccxt(self, side: str, size: float):
        try:
            if side == 'buy':
                result = self._trader.exchange.create_market_buy_order(self.symbol, size)
            else:
                result = self._trader.exchange.create_market_sell_order(self.symbol, size)
            logger.info('CCXT ORDER  %s  size=%.4f  id=%s',
                        side.upper(), size, result.get('id', '?'))
        except Exception as e:
            logger.error('CCXT order FAILED: %s', e)

    # ── Dash integration ─────────────────────────────────────────────────────────

    # POST a trade marker to the DashPlotter; silently skips if dash_url is unset.
    def _send_dash_marker(self, action: str, price: float):
        if not self.dash_url:
            return
        try:
            requests.post(
                f'{self.dash_url}/trade',
                json={
                    'action': action,
                    'price':  price,
                    'date':   datetime.now(timezone.utc).isoformat(),
                    'symbol': self.symbol,
                },
                timeout=3,
            )
        except Exception as e:
            logger.warning('Dash marker POST failed: %s', e)

    # ── CSV log ──────────────────────────────────────────────────────────────────

    # Create the CSV with a header row if it does not already exist.
    def _init_csv(self):
        path = Path(self.csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, 'w', newline='') as f:
                csv.writer(f).writerow(
                    ['timestamp', 'symbol', 'broker', 'action', 'price', 'size', 'position_after']
                )

    # Append one fill record to the CSV log.
    def _log_trade(self, action: str, price: float, size: float):
        with open(self.csv_path, 'a', newline='') as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(),
                self.symbol,
                self.broker_type,
                action,
                f'{price:.4f}',
                f'{size:.4f}',
                self.position,
            ])
