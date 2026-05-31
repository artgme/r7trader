"""
test_mozg_kraken.py — Live-trade multiple symbols with per-symbol strategy and broker.

Each symbol runs in its own thread with its own Mozg engine instance, feed
queue, and CSV log.  A single Ctrl-C stops all engines cleanly.

If test_cex_dash.py is running, entry/exit markers appear automatically on its
chart at DASH_URL for any symbol with plot=True.

WARNING
-------
paper_mode = True by default — no real orders are sent.
Set paper_mode = False only when ready for live trading.

Usage
-----
    python test_mozg_kraken.py
"""

import logging
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from configs import get_params
from mozg import Mozg
from strategies.momentum_v8 import MomentumV8Strategy
from strategies.momentum_v11 import MomentumV11Strategy
from strategies.rsi_strategy import RSIStrategy

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

PAPER_MODE = True    # set to False only when ready for real trading
DASH_URL   = 'http://127.0.0.1:8051'   # set to None if chart is not running

# ── Symbols to trade ──────────────────────────────────────────────────────────
# Each dict is one independent engine.  All fields are required.
#
#   symbol    : exchange symbol — ccxt format for crypto, plain ticker for IBKR
#   timeframe : bar size in ccxt notation ('1m', '5m', '1h', …)
#   size      : fixed units per order
#   csv       : path to the trade log file
#   plot      : True = post markers to the Dash chart; False = silent
#   strategy  : any Strategy class from the strategies/ folder
#   broker    : 'ccxt' (Kraken) or 'ibkr' (Interactive Brokers)
#
SYMBOLS = [
    {
        'symbol':   'SOL/USD',
        'timeframe':'1m',
        'size':     1.0,
        'csv':      'logs/trades_sol_v8.csv',
        'plot':     True,
        'strategy': MomentumV8Strategy,
        'broker':   'ccxt',
    },
    {
        'symbol':   'BTC/USD',
        'timeframe':'1m',
        'size':     0.001,
        'csv':      'logs/trades_btc_v11.csv',
        'plot':     True,
        'strategy': MomentumV11Strategy,
        'broker':   'ccxt',
    },
    # {
    #     'symbol':   'AAPL',
    #     'timeframe':'5m',
    #     'size':     1.0,
    #     'csv':      'logs/trades_aapl_rsi.csv',
    #     'plot':     False,
    #     'strategy': RSIStrategy,
    #     'broker':   'ibkr',
    # },
]


def _run_engine(engine: Mozg) -> None:
    engine.run(handle_sigint=False)


def main() -> None:
    engines = []

    for cfg in SYMBOLS:
        strategy = cfg['strategy']
        symbol   = cfg['symbol']
        timeframe= cfg['timeframe']

        params = get_params(strategy.__name__, symbol, timeframe)
        params['printlog'] = True

        engine = Mozg(
            symbol          = symbol,
            timeframe       = timeframe,
            strategy_class  = strategy,
            strategy_params = params,
            broker_type     = cfg['broker'],
            trade_size      = cfg['size'],
            history_limit   = 100,
            poll_interval_s = 10,
            dash_url        = DASH_URL if cfg['plot'] else None,
            csv_path        = cfg['csv'],
            paper_mode      = PAPER_MODE,
        )

        if not engine.connect():
            logging.error('Failed to connect for %s — skipping.', symbol)
            continue

        engines.append(engine)

    if not engines:
        logging.error('No engines connected. Exiting.')
        return

    threads = [
        threading.Thread(target=_run_engine, args=(e,), daemon=True,
                         name=f'mozg-{e.symbol}')
        for e in engines
    ]
    for t in threads:
        t.start()

    def _sigint(_sig, _frame):
        logging.info('Shutting down all engines…')
        for e in engines:
            e.stop()

    signal.signal(signal.SIGINT, _sigint)

    for t in threads:
        t.join()

    logging.info('All engines stopped.')
    os._exit(0)


if __name__ == '__main__':
    main()
