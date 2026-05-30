"""
test_mozg_kraken.py — Live-trade SOL/USD on Kraken using MomentumV8Strategy.

Trades exactly 1 SOL per signal.  Historical bars are preloaded to warm up
the strategy's volume SMA before any live signal can fire.

If test_cex_dash.py is already running, entry/exit markers will appear
automatically on its chart at http://127.0.0.1:8051.

WARNING
-------
This places REAL orders on Kraken.  Verify your API key has trading permissions
and your account holds sufficient SOL/USD balance before running.

Usage
-----
    python test_mozg_kraken.py
"""

import logging

from configs import get_params
from mozg import Mozg
from strategies.momentum_v8 import MomentumV8Strategy

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

SYMBOL    = 'SOL/USD'
TIMEFRAME = '1m'
SIZE      = 1.0     # 1 SOL per trade


def main():
    # load strategy parameters from configs.py for this symbol/timeframe combo
    params = get_params('MomentumV8', SYMBOL, TIMEFRAME)
    params['printlog'] = True   # runtime-only flag, not stored in configs

    engine = Mozg(
        symbol          = SYMBOL,
        timeframe       = TIMEFRAME,
        strategy_class  = MomentumV8Strategy,
        strategy_params = params,
        broker_type     = 'ccxt',
        trade_size      = SIZE,
        history_limit   = 100,   # 100 bars is enough to warm up vol_len=10 SMA
        poll_interval_s = 10,
        dash_url        = 'http://127.0.0.1:8051',  # remove if chart is not running
        csv_path        = 'trades_sol_v8.csv',
        paper_mode      = True,  # set to False only when ready for real trading
    )

    if not engine.connect():
        return

    engine.run()


if __name__ == '__main__':
    main()
