import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('rocket_janek').setLevel(logging.WARNING)  # buy_or_sell() logs INFO per call — way too noisy across a grid sweep

import ast
import datetime
import itertools
import pprint
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
from alpaca.data.historical import StockHistoricalDataClient

from backtester_alpaca import fetch_range, run_backtest, ALPACA_API_KEY, ALPACA_SECRET_KEY

FOUND_PARAMS_FILE = Path('tuner1_found_params.py')

# Top 50 from Potential_2026-07-18_81690.csv, ranked by |1-day price change %| x relative
# volume (matches what buy_or_sell() actually detects: a big move backed by unusual volume),
# capped at 8 per sector so Electronic technology/Technology services don't crowd out everything
# else — spans 14 sectors overall. Tuned one at a time, results reported per ticker.
TICKERS = [
    'ISRG', 'AA', 'ASTS', 'NXT', 'STX', 'ALAB', 'ARWR', 'A', 'HOOD', 'PSKY',
    'VSAT', 'UMC', 'AFRM', 'MXL', 'ALK', 'BE', 'REZI', 'BROS', 'NBIS', 'AAL',
    'LITE', 'RBLX', 'CDE', 'DHI', 'ENTG', 'CVNA', 'JHX', 'IREN', 'MWH', 'QXO',
    'HPQ', 'VFC', 'VSH', 'U', 'UAL', 'GLXY', 'APLD', 'CRDO', 'RIOT', 'RKT',
    'SHC', 'HL', 'LYFT', 'IVZ', 'LEN', 'CLF', 'RCL', 'APO', 'APTV', 'DAL',
]
TIMEFRAME = '30m'
START_DT = datetime.datetime(2026, 7, 1, 9, 30, tzinfo=ZoneInfo('America/New_York'))
END_DAY = datetime.date(2026, 7, 18)
QUANTITY = 10
TOP_N = 10  # how many best combos to print per ticker

# Grid to search — coarse for now, narrow in once a promising region shows up.
VOL_LEN_RANGE = [5, 7, 10]
VOL_MULTIPLIER_RANGE = [0.5, 0.7,1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5]
PRICE_MOVE_PCT_RANGE = [0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
TRAIL_STOP_PCT_RANGE = [1.0, 1.5, 2.0, 2.5, 3.0]
BODY_RATIO_THRESHOLD_RANGE = [0.3, 0.5, 0.7]


# Usage: score = score_trades(trades)
def score_trades(trades: list[dict]) -> dict:
    """Summarize one backtest run's trades. Total P&L alone can reward a combo that just
    caught one lucky trade — trade_count and win_rate come along so that can be spotted.
    expectancy (P&L per trade) rewards combos that are profitable *and* consistent, without
    the scale-mismatch or early-exit bias of weighting win_rate directly against total_pnl."""
    closed = [t for t in trades if t['pnl'] is not None]
    total_pnl = sum(t['pnl'] for t in closed)
    wins = sum(1 for t in closed if t['pnl'] > 0)
    win_rate = wins / len(closed) if closed else 0.0
    expectancy = total_pnl / len(closed) if closed else 0.0
    return {'total_pnl': total_pnl, 'trade_count': len(closed), 'win_rate': win_rate, 'expectancy': expectancy}


# Usage: results = tune_ticker(ticker, low_df, high_df)
def tune_ticker(ticker: str, low_df, high_df) -> list[dict]:
    """Grid-search every parameter combination for one ticker against already-fetched data —
    fetching happens once in main(), run_backtest() itself makes no network calls."""
    combos = list(itertools.product(VOL_LEN_RANGE, VOL_MULTIPLIER_RANGE, PRICE_MOVE_PCT_RANGE,
                                     TRAIL_STOP_PCT_RANGE, BODY_RATIO_THRESHOLD_RANGE))
    total = len(combos)
    print(f'{ticker}: grid size {total} combos '
          f'({len(VOL_LEN_RANGE)} vol_len × {len(VOL_MULTIPLIER_RANGE)} vol_multiplier × '
          f'{len(PRICE_MOVE_PCT_RANGE)} price_move_pct × {len(TRAIL_STOP_PCT_RANGE)} trail_stop_pct × '
          f'{len(BODY_RATIO_THRESHOLD_RANGE)} body_ratio_threshold)')

    results = []
    for i, (vol_len, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold) in enumerate(combos, 1):
        trades, _ = run_backtest(ticker, low_df, high_df, START_DT, TIMEFRAME, vol_len,
                                  vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, QUANTITY)
        results.append({
            'vol_len': vol_len,
            'vol_multiplier': vol_multiplier,
            'price_move_pct': price_move_pct,
            'trail_stop_pct': trail_stop_pct,
            'body_ratio_threshold': body_ratio_threshold,
            **score_trades(trades),
        })
        print(f'\r  {ticker}: {i}/{total} combos tested', end='', flush=True)
    print()  # newline after the in-place progress line
    return results


# Usage: print_results_table('RKLB', results)
def print_results_table(ticker: str, results: list[dict]) -> None:
    """Print the top TOP_N combos for one ticker, ranked by total P&L (results must already be sorted)."""
    print(f'\n=== {ticker}: top {min(TOP_N, len(results))} of {len(results)} combos, ranked by total P&L ===')
    print(f"  {'vol_len':>7}  {'vol_mult':>9}  {'price_pct':>10}  {'trail_pct':>10}  {'body_ratio':>11}  {'trades':>7}  {'win_rate':>9}  {'total_pnl':>10}  {'expectancy':>11}")
    for r in results[:TOP_N]:
        print(f"  {r['vol_len']:>7d}  {r['vol_multiplier']:>9.2f}  {r['price_move_pct']:>10.2f}  {r['trail_stop_pct']:>10.2f}  "
              f"{r['body_ratio_threshold']:>11.2f}  {r['trade_count']:>7d}  {r['win_rate']:>8.0%}  {r['total_pnl']:>+10.2f}  {r['expectancy']:>+11.2f}")


def _load_found_params() -> dict:
    """Read tuner1_found_params.py's PARAMS dict, or {} if the file doesn't exist yet or fails to parse."""
    if not FOUND_PARAMS_FILE.exists():
        return {}
    text = FOUND_PARAMS_FILE.read_text()
    try:
        _, _, dict_text = text.partition('=')
        return ast.literal_eval(dict_text.strip())
    except (SyntaxError, ValueError) as e:
        logger.warning('Could not parse %s (%s) — starting fresh.', FOUND_PARAMS_FILE, e)
        return {}


def _write_found_params(all_params: dict) -> None:
    with open(FOUND_PARAMS_FILE, 'w') as f:
        f.write('PARAMS: dict = ')
        f.write(pprint.pformat(all_params, indent=4, width=100))
        f.write('\n')


# Usage: save_best_params('RKLB', results)
def save_best_params(ticker: str, results: list[dict]) -> None:
    """Pick the combo with the best expectancy for this ticker and write it into
    tuner1_found_params.py, in the same PARAMS[strategy][ticker][timeframe] shape as
    configs_rocketJanek.py — merging with whatever's already there for other tickers."""
    best = max(results, key=lambda r: r['expectancy'])
    params = {
        'vol_len': best['vol_len'],
        'vol_multiplier': best['vol_multiplier'],
        'price_move_pct': best['price_move_pct'],
        'trail_stop_pct': best['trail_stop_pct'],
        'body_ratio_threshold': best['body_ratio_threshold'],
    }

    all_params = _load_found_params()
    all_params.setdefault('MomentumV8Strategy', {}).setdefault(ticker, {})[TIMEFRAME] = params
    _write_found_params(all_params)
    logger.info('Saved best params for %s (%s, expectancy %+.2f) to %s: %s',
                ticker, TIMEFRAME, best['expectancy'], FOUND_PARAMS_FILE, params)


# Usage: plot_3d('RKLB', results, 'vol_multiplier', 'price_move_pct', 'total_pnl')
def plot_3d(ticker: str, results: list[dict], param_x: str, param_y: str, z_metric: str) -> None:
    """3D surface of 2 tuned parameters (x, y) against a chosen performance metric (z_metric,
    'total_pnl', 'win_rate', or 'expectancy'). Each grid point is the best z_metric found
    across all values of the other 3 tuned parameters for that (x, y) combination. Builds the
    figure but doesn't show it — call plt.show() once after plotting every ticker so none of
    them block in turn."""
    if z_metric not in ('total_pnl', 'win_rate', 'expectancy'):
        raise ValueError("z_metric must be 'total_pnl', 'win_rate', or 'expectancy'")

    best = {}
    for r in results:
        key = (r[param_x], r[param_y])
        if key not in best or r[z_metric] > best[key]:
            best[key] = r[z_metric]

    xs = sorted({x for x, y in best})
    ys = sorted({y for x, y in best})
    X, Y = np.meshgrid(xs, ys)
    Z = np.array([[best[(x, y)] for x in xs] for y in ys])

    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(projection='3d')
    surf = ax.plot_surface(X, Y, Z, cmap='RdYlGn')
    ax.set_xlabel(param_x)
    ax.set_ylabel(param_y)
    ax.set_zlabel(z_metric)
    ax.set_title(f'{ticker}: best {z_metric} by {param_x} / {param_y}')
    fig.colorbar(surf, label=z_metric)


# Usage: print_ticker_ranking(results_by_ticker, 'expectancy')
def print_ticker_ranking(results_by_ticker: dict[str, list[dict]], metric: str = 'expectancy') -> None:
    """Rank tickers by their own best combo's value of `metric` (default 'expectancy'), best
    ticker first. The best combo is independently reselected using `metric` for each ticker,
    so switching metrics always reflects that metric's own best pick, not a stale one."""
    if metric not in ('total_pnl', 'win_rate', 'expectancy'):
        raise ValueError("metric must be 'total_pnl', 'win_rate', or 'expectancy'")

    best_per_ticker = {ticker: max(results, key=lambda r: r[metric]) for ticker, results in results_by_ticker.items()}
    ranked = sorted(best_per_ticker.items(), key=lambda kv: kv[1][metric], reverse=True)

    print(f'\n=== Ticker ranking by {metric} (best combo per ticker) ===')
    print(f"  {'#':>3}  {'ticker':6}  {metric:>11}  {'vol_len':>7}  {'vol_mult':>9}  {'price_pct':>10}  {'trail_pct':>10}  {'body_ratio':>11}")
    for i, (ticker, r) in enumerate(ranked, 1):
        metric_str = f"{r[metric]:>+11.0%}" if metric == 'win_rate' else f"{r[metric]:>+11.2f}"
        print(f"  {i:>3}  {ticker:6}  {metric_str}  {r['vol_len']:>7d}  {r['vol_multiplier']:>9.2f}  "
              f"{r['price_move_pct']:>10.2f}  {r['trail_stop_pct']:>10.2f}  {r['body_ratio_threshold']:>11.2f}")


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
        results.sort(key=lambda r: r['total_pnl'], reverse=True)
        print_results_table(ticker, results)
        save_best_params(ticker, results)
        results_by_ticker[ticker] = results
        #plot_3d(ticker, results, 'vol_multiplier', 'price_move_pct', 'expectancy')
        plot_3d(ticker, results, 'vol_multiplier', 'trail_stop_pct', 'expectancy')

    print_ticker_ranking(results_by_ticker, 'expectancy')
    plt.show()  # blocks once, here, after every ticker's figure has been built


if __name__ == '__main__':
    main()
