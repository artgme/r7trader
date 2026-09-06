import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('rocket_janek').setLevel(logging.WARNING)
logging.getLogger('signal_checks').setLevel(logging.WARNING)  # check_vol_price_body() logs INFO per call — way too noisy across a grid sweep

import datetime
import itertools
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import matplotlib.pyplot as plt
from alpaca.data.historical import StockHistoricalDataClient

from backtester_alpaca import fetch_range, run_backtest, ALPACA_API_KEY, ALPACA_SECRET_KEY
from logging_functions import log_tuning_csv, EXCHANGE_TZ

# Parallel counterpart to tuner1.py — same grid, same run_backtest() calls, same output files
# (tuning log, tuner1_found_params.py, plots), just spread across a ProcessPoolExecutor instead
# of run serially on one core. Everything reusable (constants, scoring, printing, saving,
# plotting) is imported straight from tuner1.py rather than duplicated; only tune_ticker()'s
# inner loop is reimplemented here.
from tuner1 import (
    score_trades, print_results_table, save_best_params, plot_3d, print_ticker_ranking,
    rank_key, SELECTION_METRIC,
    TICKERS, TIMEFRAME, START_DT, END_DAY, QUANTITY,
    VOL_LEN_RANGE, VOL_MULTIPLIER_RANGE, PRICE_MOVE_PCT_RANGE,
    TRAIL_STOP_PCT_RANGE, BODY_RATIO_THRESHOLD_RANGE, TAKE_PROFIT_PCT_RANGE,
)

MAX_WORKERS = None  # None = os.cpu_count() (one worker per logical core); lower this to leave
                     # some cores free for other work while a long sweep runs

LOG_SUFFIX = f"{datetime.datetime.now(EXCHANGE_TZ).strftime('%Y%m%d_%H%M')}_{TIMEFRAME}"
TUNING_LOG = Path(f'tuning_logs/tuning_{LOG_SUFFIX}_parallel.csv')

# Set once per worker process by _init_worker() below, then read (never written) by every
# _run_one_combo() call that worker handles. Keeping low_df/high_df here means they're pickled
# and sent to each worker exactly once, rather than re-pickled on every one of the (tens of
# thousands of) individual combos a naive "pass them as task arguments" version would send.
_worker_low_df = None
_worker_high_df = None


def _init_worker(low_df, high_df) -> None:
    """Pool initializer — runs once when each worker process starts."""
    global _worker_low_df, _worker_high_df
    _worker_low_df = low_df
    _worker_high_df = high_df


# Usage: result = _run_one_combo(ticker, vol_len, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, take_profit_pct)
def _run_one_combo(ticker: str, vol_len: int, vol_multiplier: float, price_move_pct: float,
                    trail_stop_pct: float, body_ratio_threshold: float, take_profit_pct: float) -> dict:
    """Runs inside a worker process: one run_backtest() call for one grid combo, against the
    low_df/high_df this worker was handed once by _init_worker(). Returns the same per-combo
    result dict shape tuner1.tune_ticker() has always built, so everything downstream (sorting,
    printing, saving, plotting) works unmodified on the results this produces."""
    trades, _ = run_backtest(ticker, _worker_low_df, _worker_high_df, START_DT, TIMEFRAME, vol_len,
                              vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, QUANTITY,
                              take_profit_pct=take_profit_pct)
    return {
        'vol_len': vol_len,
        'vol_multiplier': vol_multiplier,
        'price_move_pct': price_move_pct,
        'trail_stop_pct': trail_stop_pct,
        'body_ratio_threshold': body_ratio_threshold,
        'take_profit_pct': take_profit_pct,
        **score_trades(trades),
    }

# Usage: results = tune_ticker(ticker, low_df, high_df)
def tune_ticker(ticker: str, low_df, high_df) -> list[dict]:
    """Grid-search every parameter combination for one ticker, spread across MAX_WORKERS worker
    processes (default: one per CPU core). low_df/high_df are fetched once in main() exactly as
    in tuner1.py — this function still makes no network calls itself."""
    combos = list(itertools.product(VOL_LEN_RANGE, VOL_MULTIPLIER_RANGE, PRICE_MOVE_PCT_RANGE,
                                     TRAIL_STOP_PCT_RANGE, BODY_RATIO_THRESHOLD_RANGE, TAKE_PROFIT_PCT_RANGE))
    total = len(combos)
    workers = MAX_WORKERS or os.cpu_count()
    print(f'{ticker}: grid size {total} combos '
          f'({len(VOL_LEN_RANGE)} vol_len × {len(VOL_MULTIPLIER_RANGE)} vol_multiplier × '
          f'{len(PRICE_MOVE_PCT_RANGE)} price_move_pct × {len(TRAIL_STOP_PCT_RANGE)} trail_stop_pct × '
          f'{len(BODY_RATIO_THRESHOLD_RANGE)} body_ratio_threshold × '
          f'{len(TAKE_PROFIT_PCT_RANGE)} take_profit_pct), {workers} worker process(es)')

    results = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS, initializer=_init_worker, initargs=(low_df, high_df)) as executor:
        futures = [executor.submit(_run_one_combo, ticker, *combo) for combo in combos]
        # as_completed() yields whichever future finishes next, not in submission order — fine,
        # since `results` gets sorted by total_pnl afterwards anyway either way.
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            print(f'\r  {ticker}: {i}/{total} combos tested', end='', flush=True)
    print()  # newline after the in-place progress line
    return results


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.error('Set ALPACA_API_KEY and ALPACA_API_SECRET in .env before running this.')
        return
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    # Buffer must cover the largest vol_len in the grid, not just a fixed guess.
    fetch_start_day = START_DT.date() - datetime.timedelta(days=max(VOL_LEN_RANGE) + 5)

    results_by_ticker = {}
    for ticker in TICKERS:
        low_df = fetch_range(client, ticker, fetch_start_day, END_DAY, TIMEFRAME)
        high_df = fetch_range(client, ticker, fetch_start_day, END_DAY, '1m')
        if low_df.empty or high_df.empty:
            logger.warning('No data for %s — skipping.', ticker)
            continue

        results = tune_ticker(ticker, low_df, high_df)
        log_tuning_csv(TUNING_LOG, ticker, TIMEFRAME, START_DT, END_DAY, results)
        results.sort(key=rank_key(SELECTION_METRIC), reverse=True)
        print_results_table(ticker, results)
        save_best_params(ticker, results)
        results_by_ticker[ticker] = results
        plot_3d(ticker, results, 'vol_multiplier', 'trail_stop_pct', 'expectancy')

    print_ticker_ranking(results_by_ticker)
    plt.show()  # blocks once, here, after every ticker's figure has been built


# Required on macOS/Windows: ProcessPoolExecutor uses the 'spawn' start method there, which
# re-imports this module fresh in every worker process. Without this guard, that re-import
# would re-trigger main() in each worker too, recursively spawning more pools.
if __name__ == '__main__':
    main()
