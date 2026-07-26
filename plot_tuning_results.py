import logging
from pathlib import Path

import matplotlib.pyplot as plt

from logging_functions import load_tuning_log
from tuner1 import plot_3d, print_ticker_ranking

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s")
logger = logging.getLogger(__name__)

# Ticker(s) to render as a 3D surface; leave empty to just print the ranking table.
TICKERS_TO_PLOT = ['VOYG']
PARAM_X = 'vol_multiplier'
PARAM_Y = 'price_move_pct' #'trail_stop_pct
Z_METRIC = 'expectancy'  # 'total_pnl', 'win_rate', or 'expectancy'


# Usage: log_path = latest_tuning_log()
def latest_tuning_log(log_dir: Path = Path('tuning_logs')) -> Path | None:
    """Most recently written tuning_*.csv in log_dir, or None if the folder is empty/missing."""
    logs = sorted(log_dir.glob('tuning_*.csv'), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def main():
    # Point this at a specific file instead if you don't want the latest run, e.g.:
    # log_path = Path('tuning_logs/tuning_30m_20260726_1400.csv')
    log_path = latest_tuning_log()
    if log_path is None:
        logger.error('No tuning logs found in tuning_logs/ — run tuner1.py first.')
        return

    logger.info('Loading %s', log_path)
    results_by_ticker = load_tuning_log(log_path)
    print_ticker_ranking(results_by_ticker, Z_METRIC)

    for ticker in TICKERS_TO_PLOT:
        if ticker not in results_by_ticker:
            logger.warning('%s not found in %s — skipping.', ticker, log_path)
            continue
        plot_3d(ticker, results_by_ticker[ticker], PARAM_X, PARAM_Y, Z_METRIC)

    if TICKERS_TO_PLOT:
        plt.show()


if __name__ == '__main__':
    main()
