"""
Live crypto chart via Kraken (ccxt) + Dash.

Thread layout
─────────────
  main thread   – keeps process alive; Ctrl-C triggers clean shutdown
  cex-worker    – fetches historical bars, then streams live bars via polling;
                  pushes every update to the plotter queue
  dash-plotter  – Werkzeug/Dash server; drains the queue on each 1-second interval callback
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

from dash_plot import DashPlotter
from cex import CexTrader

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# symbol must match Kraken's ccxt market id (base/quote)
SYMBOL = 'SOL/USD'
TIMEFRAME = '1m' # ccxt timeframes: '1m', '5m', '15m', '30m', '1h', '4h', '1d', etc.; must be supported by the exchange
HISTORY_LIMIT = 100      # how many bars to download from Kraken on start-up
DISPLAY_BARS = 0        # how many bars to show on the x-axis (0 = all); older bars scroll off
POLL_INTERVAL_S = 10     # seconds between live polls (keep above Kraken rate limits)


# ccxt returns timestamps in milliseconds; convert to a tz-aware datetime for DashPlotter
def _bar_to_datetime(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def cex_worker(plotter: DashPlotter, stop_event: threading.Event) -> None:
    trader = CexTrader()
    try:
        # authenticate with Kraken using keys from .env; abort the whole app on failure
        if not trader.connect():
            logger.error('Failed to connect to Kraken')
            stop_event.set()
            return

        # one-shot download of the most recent HISTORY_LIMIT bars so the chart
        # is populated immediately when the browser is opened
        historical = trader.fetch_historical_ohlcv(SYMBOL, TIMEFRAME, limit=HISTORY_LIMIT)
        if historical:
            for bar in historical:
                ts, o, h, l, c, v = bar
                plotter.push(_bar_to_datetime(ts), o, h, l, c, v)
            logger.info('Loaded %d historical bars for %s [%s]', len(historical), SYMBOL, TIMEFRAME)

        logger.info('Streaming live bars every %d s…', POLL_INTERVAL_S)

        # called by stream_live_ohlcv for every new bar; converts the raw ccxt list
        # to named values and forwards them to the thread-safe DashPlotter queue
        def on_bar(bar):
            ts, o, h, l, c, v = bar
            date = _bar_to_datetime(ts)
            plotter.push(date, o, h, l, c, v)
            logger.info('Bar  %s  O=%.2f H=%.2f L=%.2f C=%.2f  V=%.4f', date, o, h, l, c, v)

        # blocks here until stop_event is set (by Ctrl-C in main); polls Kraken
        # every POLL_INTERVAL_S seconds and fires on_bar for each new timestamp seen
        trader.stream_live_ohlcv(
            SYMBOL,
            TIMEFRAME,
            callback=on_bar,
            stop_event=stop_event,
            seed_bars=3,
            poll_interval_s=POLL_INTERVAL_S,
        )

    except Exception:
        logger.exception('CEX worker error')
        stop_event.set()


def main() -> None:
    # shared flag: setting it tells the cex-worker loop to exit
    stop_event = threading.Event()

    # start the Dash server in its own daemon thread; port 8051 avoids conflict
    # with the IBKR test that runs on 8050
    plotter = DashPlotter(title=f'{SYMBOL} – {TIMEFRAME} Live (Kraken)', port=8051, display_bars=DISPLAY_BARS)
    plotter.start()
    time.sleep(1)  # give Werkzeug a moment to bind the port before logging the URL
    logger.info('Chart → http://127.0.0.1:8051')

    # cex-worker is a daemon so it is killed automatically if the main thread exits
    worker = threading.Thread(
        target=cex_worker,
        args=(plotter, stop_event),
        daemon=True,
        name='cex-worker',
    )
    worker.start()

    # keep the main thread alive; Ctrl-C sets stop_event and waits for the worker
    # to finish its current poll before os._exit() tears down all threads
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info('Shutting down…')
        stop_event.set()
        worker.join(timeout=5)
        os._exit(0)


if __name__ == '__main__':
    main()
