"""
Live crypto chart via Kraken (ccxt) + Dash.

One data worker thread is started per symbol.  A dropdown in the chart lets
you switch between symbols without reloading the page.

Thread layout
─────────────
  main thread         – keeps process alive; Ctrl-C triggers clean shutdown
  cex-worker-<sym>    – one per symbol; fetches historical bars then streams live;
                        pushes every update to the plotter queue tagged with symbol
  dash-plotter        – Werkzeug/Dash server; drains the queue on each 1-second callback
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dash_plot import DashPlotter
from cex import CexTrader

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ── Symbols to chart ──────────────────────────────────────────────────────────
# Each entry: (symbol, timeframe)
# Add or remove rows to change which symbols appear in the dropdown.
SYMBOLS_TO_CHART = [
    ('SOL/USD', '1m'),
    ('BTC/USD', '1m'),
    # ('ETH/USD', '1m'),
]

HISTORY_LIMIT   = 100   # bars downloaded per symbol on start-up
DISPLAY_BARS    = 0     # 0 = show all; >0 keeps only the last N candles visible
POLL_INTERVAL_S = 30    # seconds between live polls (keep above Kraken rate limits)


# ccxt returns timestamps in milliseconds; convert to a tz-aware datetime for DashPlotter
def _bar_to_datetime(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


# One worker per symbol: connects to Kraken, preloads history, then streams live bars.
# Each bar is tagged with the symbol so DashPlotter stores it in the correct DataFrame.
def cex_worker(symbol: str, timeframe: str,
               plotter: DashPlotter, stop_event: threading.Event) -> None:
    trader = CexTrader()
    try:
        if not trader.connect():
            logger.error('[%s] Failed to connect to Kraken', symbol)
            stop_event.set()
            return

        historical = trader.fetch_historical_ohlcv(symbol, timeframe, limit=HISTORY_LIMIT)
        if historical:
            for bar in historical:
                ts, o, h, l, c, v = bar
                plotter.push(_bar_to_datetime(ts), o, h, l, c, v, symbol=symbol)
            logger.info('[%s] Loaded %d historical bars', symbol, len(historical))

        logger.info('[%s] Streaming live bars every %d s…', symbol, POLL_INTERVAL_S)

        def on_bar(bar):
            ts, o, h, l, c, v = bar
            date = _bar_to_datetime(ts)
            plotter.push(date, o, h, l, c, v, symbol=symbol)
            logger.info('[%s] Bar %s  C=%.4f', symbol, date, c)

        trader.stream_live_ohlcv(
            symbol, timeframe,
            callback=on_bar,
            stop_event=stop_event,
            seed_bars=3,
            poll_interval_s=POLL_INTERVAL_S,
        )

    except Exception:
        logger.exception('[%s] CEX worker error', symbol)
        stop_event.set()


def main() -> None:
    stop_event = threading.Event()

    symbols_str = ', '.join(s for s, _ in SYMBOLS_TO_CHART)
    plotter = DashPlotter(
        title        = f'Live Chart – {symbols_str} (Kraken)',
        port         = 8051,
        display_bars = DISPLAY_BARS,
    )
    plotter.start()
    time.sleep(1)
    logger.info('Chart → http://127.0.0.1:8051')

    # one data worker per symbol
    workers = []
    for symbol, timeframe in SYMBOLS_TO_CHART:
        w = threading.Thread(
            target = cex_worker,
            args   = (symbol, timeframe, plotter, stop_event),
            daemon = True,
            name   = f'cex-worker-{symbol}',
        )
        w.start()
        workers.append(w)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info('Shutting down…')
        stop_event.set()
        for w in workers:
            w.join(timeout=5)
        os._exit(0)


if __name__ == '__main__':
    main()
