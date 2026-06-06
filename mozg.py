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

# ccxt timeframe string → IBKR bar-size string
_IBKR_BAR_SIZE: dict = {
    '1m': '1 min', '5m': '5 mins', '15m': '15 mins',
    '30m': '30 mins', '1h': '1 hour', '4h': '4 hours', '1d': '1 day',
}


# ─── Custom backtrader live feed ──────────────────────────────────────────────

class _QueueFeed(bt.feed.DataBase):
    """
    Backtrader DataBase feed backed by a thread-safe queue.
    Each item in the queue: (datetime, open, high, low, close, volume).
    Pushing None is the stop sentinel — _load() returns False and cerebro exits.
    """
    params = (
        ('queue', None),     # queue.Queue instance supplied by Mozg
        ('live_timeout', 5), # seconds to block per _load() call before returning None
    )

    def islive(self):
        return True

    def haslivedata(self):
        return not self.p.queue.empty()

    def _load(self):
        try:
            bar = self.p.queue.get(timeout=self.p.live_timeout)
        except queue.Empty:
            return None  # no bar yet; cerebro will call _load again shortly
        if bar is None:
            return False  # stop sentinel; cerebro will shut down
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
    Return a subclass of strategy_class that calls on_fill_cb(is_buy, size, price)
    each time bt's internal broker completes an order.  The original notify_order
    is still called first so the strategy's own P&L tracking stays intact.
    """
    class _Wrapped(strategy_class):
        def notify_order(self, order):
            super().notify_order(order)
            if order.status == order.Completed:
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

        # public — readable from outside at any time
        self.position: str = FLAT   # 'flat' | 'long' | 'short'

        # private
        self._feed_queue:        queue.Queue   = queue.Queue()
        self._stop_event:        threading.Event = threading.Event()
        self._ibkr_order_queue:  queue.Queue   = queue.Queue()
        self._trader                            = None   # CexTrader or IBKRGateway
        self._ibkr_contract                     = None
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
        from ibkr import IBKRGateway
        self._trader = IBKRGateway(client_id=82)
        if not self._trader.connect():
            return False
        self._ibkr_contract = self._trader.make_stock_contract(self.symbol)
        self.position = self._sync_position_ibkr()
        logger.info('Starting position: %s', self.position)
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

        feed = _QueueFeed(queue=self._feed_queue)

        cerebro = bt.Cerebro(runonce=False)
        cerebro.adddata(feed)
        cerebro.addstrategy(live_cls, **self.strategy_params)
        cerebro.addsizer(bt.sizers.FixedSize, stake=self.trade_size)
        cerebro.broker.setcash(self.starting_cash)

        # mirror the real broker's starting position inside bt's paper broker
        if self.position == LONG:
            cerebro.broker.setposition(feed, size=self.trade_size, price=0.0)
        elif self.position == SHORT:
            cerebro.broker.setposition(feed, size=-self.trade_size, price=0.0)

        self._init_csv()

        target = self._ccxt_data_worker if self.broker_type == 'ccxt' else self._ibkr_data_worker
        worker = threading.Thread(target=target, daemon=True, name='data-worker')
        worker.start()

        if handle_sigint:
            def _sigint(sig, frame):
                logger.info('Stopping…')
                self.stop()
            signal.signal(signal.SIGINT, _sigint)

        logger.info(
            'Mozg running  symbol=%s  timeframe=%s  strategy=%s  broker=%s  size=%s',
            self.symbol, self.timeframe, self.strategy_class.__name__,
            self.broker_type, self.trade_size,
        )
        cerebro.run()
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

        def on_bar(bar):
            ts_ms, o, h, l, c, v = bar
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            self._last_price = float(c)
            self._feed_queue.put((dt, o, h, l, c, v))

        self._trader.stream_live_ohlcv(
            self.symbol, self.timeframe,
            callback=on_bar,
            stop_event=self._stop_event,
            seed_bars=3,
            poll_interval_s=self.poll_interval_s,
        )

    # IBKR worker: preload historical bars, subscribe to live bars, and drain the
    # order queue (orders are posted from the main thread via _ibkr_order_queue).
    def _ibkr_data_worker(self):
        from ib_insync import util
        util.startLoop()

        bar_size = _IBKR_BAR_SIZE.get(self.timeframe, '1 min')

        hist = self._trader.fetch_historical(
            self._ibkr_contract, duration='2 D',
            bar_size=bar_size, what_to_show='TRADES',
        )
        if hist:
            for bar in hist[-self.history_limit:]:
                ts = pd.Timestamp(bar.date)
                ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
                dt = ts.to_pydatetime()
                self._feed_queue.put((dt, bar.open, bar.high, bar.low, bar.close,
                                      getattr(bar, 'volume', 0.0)))
            logger.info('Preloaded %d IBKR bars.', min(len(hist), self.history_limit))

        live = self._trader.fetch_live_bars(
            self._ibkr_contract, duration='1 D',
            bar_size=bar_size, what_to_show='TRADES',
        )

        def on_bar_update(bars, has_new_bar):
            if has_new_bar:
                bar = bars[-1]
                ts = pd.Timestamp(bar.date)
                ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
                dt = ts.to_pydatetime()
                self._last_price = float(bar.close)
                self._feed_queue.put((dt, bar.open, bar.high, bar.low, bar.close,
                                      getattr(bar, 'volume', 0.0)))

        live.updateEvent += on_bar_update

        # run ib_insync event loop; drain order requests posted from the main thread
        while not self._stop_event.is_set():
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

        self._trader.disconnect()

    # ── Order intercept ──────────────────────────────────────────────────────────

    # Called by the wrapped strategy each time bt's paper broker fills an order.
    # Classifies the action, routes the real market order, updates position state,
    # sends a Dash marker, and appends a row to the CSV log.
    def _on_bt_fill(self, is_buy: bool, size: float, price: float):
        self._last_price = price
        ep = self._entry_price   # entry price recorded when we opened the position

        if is_buy:
            if self.position == FLAT:
                action, new_position = 'enter_long', LONG
                self._entry_price = price
            else:
                # closing a short: profit if fill price < entry price
                action       = 'exit_short_tp' if (ep is not None and price < ep) else 'exit_short_sl'
                new_position = FLAT
                self._entry_price = None
        else:
            if self.position == FLAT:
                action, new_position = 'enter_short', SHORT
                self._entry_price = price
            else:
                # closing a long: profit if fill price > entry price
                action       = 'exit_long_tp' if (ep is not None and price > ep) else 'exit_long_sl'
                new_position = FLAT
                self._entry_price = None

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

        self.position = new_position
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
