"""
Live 1-minute RKLB chart via IBKR + Dash.

Thread layout
─────────────
  main thread   – keeps process alive; Ctrl-C triggers clean shutdown
  ibkr-worker   – subscribes to live 1-min bars; pushes every update to the plotter queue
  dash-plotter  – Werkzeug/Dash server; drains the queue on each 1-second interval callback
"""

import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ib_insync import util

from dash_plot import DashPlotter
from ibkr import IBKRGateway

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

SYMBOL = 'EURUSD'


def ibkr_worker(plotter: DashPlotter, stop_event: threading.Event) -> None:
    util.startLoop()
    gateway = IBKRGateway(client_id=79)
    try:
        if not gateway.connect():
            logger.error('Failed to connect to IBKR')
            stop_event.set()
            return

        contract = gateway.make_forex_contract(SYMBOL)
        bars = gateway.fetch_live_bars(contract, duration='900 S', bar_size='1 min', what_to_show='MIDPOINT')

        plotter.push_bars(bars)
        logger.info('Loaded %d historical bars; streaming live 1-min updates…', len(bars))

        def on_bar_update(bars, has_new_bar):
            bar = bars[-1]
            plotter.push(bar.date, bar.open, bar.high, bar.low, bar.close, getattr(bar, 'volume', 0.0))
            if has_new_bar:
                logger.info('New bar  %s  O=%.2f H=%.2f L=%.2f C=%.2f', bar.date, bar.open, bar.high, bar.low, bar.close)

        bars.updateEvent += on_bar_update

        while not stop_event.is_set():
            gateway.ib.sleep(1)

    except Exception:
        logger.exception('IBKR worker error')
        stop_event.set()
    finally:
        gateway.disconnect()
        logger.info('IBKR disconnected')


def main() -> None:
    stop_event = threading.Event()

    plotter = DashPlotter(title=f'{SYMBOL} – 1-Minute Live', port=8050, display_hours=0)
    plotter.start()
    time.sleep(1)
    logger.info('Chart → http://127.0.0.1:8050')

    worker = threading.Thread(
        target=ibkr_worker,
        args=(plotter, stop_event),
        daemon=True,
        name='ibkr-worker',
    )
    worker.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info('Shutting down…')
        stop_event.set()
        worker.join(timeout=3)
        os._exit(0)


if __name__ == '__main__':
    main()
