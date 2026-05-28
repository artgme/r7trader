"""
Integration test: connect IBKR, fetch RKLB bars, stream them to a live Dash chart.

Thread layout
─────────────
  main thread   – starts Dash plotter (daemon) and IBKR worker thread, then
                  blocks on a stop_event so Ctrl-C works cleanly
  ibkr-worker   – all IBKR I/O lives here; pushes bars into DashPlotter's queue
  dash-plotter  – Werkzeug/Dash server; reads the queue on every 1-second
                  interval callback and redraws the chart
"""

import logging
import signal
import sys
import threading
import time

from ib_insync import util

from dash_plot import DashPlotter
from ibkr import IBKRGateway

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

SYMBOL = 'RKLB'
DURATION = '5 D'
BAR_SIZE = '30 mins'


def ibkr_worker(plotter: DashPlotter, stop_event: threading.Event) -> None:
    # ib_insync uses asyncio internally; ensure a fresh event loop in this thread
    util.startLoop()

    gateway = IBKRGateway()
    try:
        if not gateway.connect():
            logger.error('Failed to connect to IBKR – is TWS / Gateway running on port %s?', gateway.port)
            stop_event.set()
            return

        contract = gateway.make_stock_contract(SYMBOL)
        logger.info('Fetching %s historical data (%s, %s)…', SYMBOL, DURATION, BAR_SIZE)
        bars = gateway.fetch_historical(contract, duration=DURATION, bar_size=BAR_SIZE)
        logger.info('Received %d bars – pushing to chart', len(bars))
        plotter.push_bars(bars)

        # ── Demonstrate that IBKR work continues independently of the chart ──
        logger.info('IBKR thread continuing other work while Dash plots in the background…')
        for tick in range(1, 11):
            if stop_event.is_set():
                break
            time.sleep(3)
            logger.info('IBKR heartbeat %d/10 (chart still live at http://127.0.0.1:8050)', tick)

    except Exception:
        logger.exception('IBKR worker error')
    finally:
        gateway.disconnect()
        logger.info('IBKR worker done')
        stop_event.set()


def main() -> None:
    stop_event = threading.Event()

    def _handle_signal(_sig, _frame):
        logger.info('Interrupt received – shutting down')
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    plotter = DashPlotter(title=f'{SYMBOL} – Historical Bars', port=8050)
    plotter.start()
    # Give Werkzeug a moment to bind the port before printing the URL
    time.sleep(1)
    logger.info('Chart available at http://127.0.0.1:8050  (open now – it updates automatically)')

    worker = threading.Thread(
        target=ibkr_worker,
        args=(plotter, stop_event),
        daemon=True,
        name='ibkr-worker',
    )
    worker.start()

    # Block main thread until IBKR worker finishes or Ctrl-C is pressed;
    # the Dash server keeps running as a daemon thread throughout.
    stop_event.wait()
    worker.join(timeout=5)

    logger.info('Chart still live – press Ctrl-C to exit')
    try:
        while True:
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        logger.info('Exiting')
        sys.exit(0)


if __name__ == '__main__':
    main()
