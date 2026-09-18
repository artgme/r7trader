import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('rocket_janek').setLevel(logging.WARNING)
logging.getLogger('signal_checks').setLevel(logging.WARNING)  # check_vol_price_body() logs INFO per call — way too noisy across a grid sweep

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
from logging_functions import log_tuning_csv, EXCHANGE_TZ

FOUND_PARAMS_FILE = Path('tuner1_found_params6.py')

# Top 50 from Potential_2026-07-18_81690.csv, ranked by |1-day price change %| x relative
# volume (matches what check_vol_price_body() actually detects: a big move backed by unusual volume),
# capped at 8 per sector so Electronic technology/Technology services don't crowd out everything
# else — spans 14 sectors overall. Tuned one at a time, results reported per ticker.
#TICKERS = ['VOYG', 'ISRG','ASTS']  # tuned one at a time, results reported per ticker
#TICKERS = ['ELVN','HPQ','PYPL','CSCO','PATH','DIS','LUV','UAA','XP','JD','ABNB','FRSH','TMO','HALO','ISRG']
#TICKERS = ['AA', 'ASTS', 'NXT', 'STX']
TICKERS = ['ALAB', 'ARWR', 'A', 'HOOD', 'PSKY',
            'VSAT', 'UMC', 'AFRM', 'MXL', 'ALK', 'BE', 'REZI', 'BROS', 'NBIS', 'AAL',
            'LITE', 'RBLX', 'CDE', 'DHI', 'ENTG', 'CVNA', 'JHX', 'IREN', 'MWH', 'QXO',
            'HPQ', 'VFC', 'VSH', 'U', 'UAL', 'GLXY', 'APLD', 'CRDO', 'RIOT', 'RKT',
            'SHC', 'HL', 'LYFT', 'IVZ', 'LEN', 'CLF', 'RCL', 'APO', 'APTV', 'DAL']
TIMEFRAME = '10m'
START_DT = datetime.datetime(2026, 6, 1, 9, 30, tzinfo=ZoneInfo('America/New_York'))
END_DAY = datetime.date(2026, 9, 4)
QUANTITY = 10 #ile akcji kupujemy na trade
TOP_N = 10  # how many best combos to print per ticker

LOG_SUFFIX = f"{datetime.datetime.now(EXCHANGE_TZ).strftime('%Y%m%d_%H%M')}_{TIMEFRAME}"
TUNING_LOG = Path(f'tuning_logs/tuning_{LOG_SUFFIX}.csv')

# Grid to search — coarse for now, narrow in once a promising region shows up.
VOL_LEN_RANGE = [5, 7, 10]
VOL_MULTIPLIER_RANGE = [0.5, 0.7, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5]
PRICE_MOVE_PCT_RANGE = [0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5]
TRAIL_STOP_PCT_RANGE = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
BODY_RATIO_THRESHOLD_RANGE = [0.3, 0.5, 0.7]
TAKE_PROFIT_PCT_RANGE = [1.0, 1.5, 2.0]  # starting range — narrow in once a promising region shows up


# --- score_trades / combined_score tuning knobs ------------------------------------------------
MIN_TRADES_FULL_CONFIDENCE = 30  # trade_count at which combined_score stops being discounted for
                                  # a small sample (it ramps linearly from 0 trades up to here)
# Reference "this is a solid combo" values — each risk-adjusted term of combined_score is divided
# by its reference and clipped to [-2, 3] before weighting, so the three land on a common scale
# and no single blown-up ratio can dominate. Raise a reference to make that term harder to max out.
REF_SORTINO = 0.3          # per-trade Sortino ratio
REF_RECOVERY = 3.0         # total P&L as a multiple of max drawdown
REF_PROFIT_FACTOR = 2.5    # gross wins / gross losses
W_SORTINO, W_RECOVERY, W_PROFIT_FACTOR = 0.40, 0.35, 0.25  # blend weights, should sum to 1
_SCORE_KEYS = ('total_pnl', 'trade_count', 'win_rate', 'expectancy', 'max_drawdown',
               'profit_factor', 'sharpe', 'sortino', 'recovery_factor', 'combined_score')
_LOWER_IS_BETTER = ('max_drawdown',)  # every other score key is higher-is-better

# The one metric save_best_params(), the per-ticker result sort, and print_ticker_ranking() all
# rank by. Change this to retarget what "best" means — any of _SCORE_KEYS (e.g. 'expectancy',
# 'sortino', 'combined_score').
SELECTION_METRIC = 'combined_score'  #combined_score, total_pnl, win_rate, expectancy, max_drawdown, profit_factor, sharpe, sortino, recovery_factor
# ---------------------------------------------------------------------------------------------

# --- directional (long/short) tuning ------------------------------------------------------------
# If True, tune long and short entries independently via check_vol_price_body_dir() — each gets
# its own vol_multiplier/price_move_pct/trail_stop_pct/body_ratio_threshold/take_profit_pct.
# vol_len stays shared (it sizes the fetch/window itself, not a threshold) — see save_best_params()
# for how it's chosen. If False (default), today's single joint grid — one shared param set for
# both directions, unchanged from before this was added.
TUNE_DIRECTIONAL = True
# A candle can only ever fire BUY or SELL, never both, so long and short can be swept independently
# instead of jointly (which would square the grid to ~87M combos/ticker instead of ~32,400).
# DISABLED_BODY_RATIO_THRESHOLD forces the side not being swept to never fire: body_ratio is always
# in [0, 1], so a threshold above 1 guarantees green_body is False for that side, regardless of its
# other (unused, arbitrary) parameter values.
DISABLED_BODY_RATIO_THRESHOLD = 1.1
# ---------------------------------------------------------------------------------------------


# Usage: results.sort(key=rank_key('combined_score'), reverse=True)
def rank_key(metric: str):
    """A sort/max key for `metric` that always means 'best first' under reverse=True / max() —
    the sign is flipped for the lower-is-better metrics (max_drawdown) so callers never special-case."""
    if metric not in _SCORE_KEYS:
        raise ValueError(f"metric must be one of {_SCORE_KEYS}")
    sign = -1 if metric in _LOWER_IS_BETTER else 1
    return lambda r: sign * r[metric]


# Usage: score = score_trades(trades)
def score_trades(trades: list[dict]) -> dict:
    """Summarize one backtest run's trades into a dict of performance metrics (keys: _SCORE_KEYS).

    Base:  total_pnl / trade_count / win_rate / expectancy (P&L per trade) — headline P&L and
           how it was earned. Total P&L alone can reward a combo that caught one lucky trade;
           trade_count and win_rate let that be spotted.
    Risk:  max_drawdown    — deepest peak-to-trough dip (in $) of the cumulative-P&L curve with
                             trades walked in order; the worst unrealized loss you'd sit through.
           profit_factor   — gross wins / gross losses; >1 profitable, 2+ strong. inf if there is
                             no losing trade (sorts to the top — filter it when that matters).
           sharpe / sortino — mean per-trade return over its std (Sharpe) or over downside
                             deviation only (Sortino). Per-trade return is P&L / capital-tied-up,
                             so it's comparable across price levels. Risk-free rate 0, NOT
                             annualized — per-trade ratios for ranking combos, not annual figures.
           recovery_factor — total_pnl / max_drawdown (Calmar-style): profit per $ of worst dip.
    Blend: combined_score  — the single number to rank combos by: a small-sample-discounted,
                             weighted blend of Sortino, recovery_factor and profit_factor, each
                             scaled against its REF_* value (see the module constants above).
                             Negative for losing combos, pulled toward 0 for under-traded ones."""
    closed = [t for t in trades if t['pnl'] is not None]
    n = len(closed)
    if n == 0:
        return {**dict.fromkeys(_SCORE_KEYS, 0.0), 'trade_count': 0}

    pnl = np.array([t['pnl'] for t in closed], dtype=float)
    capital = np.array([t['entry_price'] * t['quantity'] for t in closed], dtype=float)
    ret = pnl / capital  # per-trade return on the capital that trade tied up

    total_pnl = float(pnl.sum())
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    win_rate = len(wins) / n
    expectancy = total_pnl / n

    equity = np.cumsum(pnl)
    max_drawdown = float((np.maximum.accumulate(equity) - equity).max())

    gross_loss = float(-losses.sum())
    profit_factor = float(wins.sum()) / gross_loss if gross_loss > 0 else np.inf

    ret_std = ret.std(ddof=1) if n > 1 else 0.0
    sharpe = float(ret.mean() / ret_std) if ret_std > 0 else 0.0
    downside = ret[ret < 0]
    downside_dev = float(np.sqrt(np.sum(downside ** 2) / n)) if downside.size else 0.0
    sortino = float(ret.mean() / downside_dev) if downside_dev > 0 else 0.0

    recovery_factor = total_pnl / max_drawdown if max_drawdown > 0 else np.inf

    # Blend: scale each term against its reference, clip, weight, discount for small samples.
    # A non-finite ratio (no losing trade / no drawdown) is treated as "way past solid" → clip ceiling.
    sample_conf = min(n / MIN_TRADES_FULL_CONFIDENCE, 1.0)
    t_sortino = np.clip(sortino / REF_SORTINO, -2.0, 3.0)
    t_recovery = np.clip((recovery_factor if np.isfinite(recovery_factor) else 9 * REF_RECOVERY) / REF_RECOVERY, -2.0, 3.0)
    t_pf = np.clip(((profit_factor if np.isfinite(profit_factor) else 9 * REF_PROFIT_FACTOR) - 1.0) / (REF_PROFIT_FACTOR - 1.0), -2.0, 3.0)
    combined_score = float(sample_conf * (W_SORTINO * t_sortino + W_RECOVERY * t_recovery + W_PROFIT_FACTOR * t_pf))

    return {
        'total_pnl': total_pnl,
        'trade_count': n,
        'win_rate': win_rate,
        'expectancy': expectancy,
        'max_drawdown': max_drawdown,
        'profit_factor': float(profit_factor),
        'sharpe': sharpe,
        'sortino': sortino,
        'recovery_factor': float(recovery_factor),
        'combined_score': combined_score,
    }


# Usage: results = tune_ticker(ticker, low_df, high_df)
def tune_ticker(ticker: str, low_df, high_df) -> list[dict]:
    """Grid-search every parameter combination for one ticker against already-fetched data —
    fetching happens once in main(), run_backtest() itself makes no network calls.

    TUNE_DIRECTIONAL=False (default): one joint grid, one shared param set for both directions
    (today's behavior, unchanged) — every result gets 'direction': 'shared'.
    TUNE_DIRECTIONAL=True: two independent sweeps per vol_len — long entries with short disabled,
    then short entries with long disabled — every result gets 'direction': 'long' or 'short'.
    See save_best_params() for how the two sweeps and the shared vol_len get combined into one
    saved params dict."""
    if not TUNE_DIRECTIONAL:
        combos = list(itertools.product(VOL_LEN_RANGE, VOL_MULTIPLIER_RANGE, PRICE_MOVE_PCT_RANGE,
                                         TRAIL_STOP_PCT_RANGE, BODY_RATIO_THRESHOLD_RANGE, TAKE_PROFIT_PCT_RANGE))
        total = len(combos)
        print(f'{ticker}: grid size {total} combos '
              f'({len(VOL_LEN_RANGE)} vol_len × {len(VOL_MULTIPLIER_RANGE)} vol_multiplier × '
              f'{len(PRICE_MOVE_PCT_RANGE)} price_move_pct × {len(TRAIL_STOP_PCT_RANGE)} trail_stop_pct × '
              f'{len(BODY_RATIO_THRESHOLD_RANGE)} body_ratio_threshold × '
              f'{len(TAKE_PROFIT_PCT_RANGE)} take_profit_pct)')

        results = []
        for i, (vol_len, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, take_profit_pct) in enumerate(combos, 1):
            trades, _ = run_backtest(ticker, low_df, high_df, START_DT, TIMEFRAME, vol_len,
                                      vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, QUANTITY,
                                      take_profit_pct=take_profit_pct)
            results.append({
                'direction': 'shared',
                'vol_len': vol_len,
                'vol_multiplier': vol_multiplier,
                'price_move_pct': price_move_pct,
                'trail_stop_pct': trail_stop_pct,
                'body_ratio_threshold': body_ratio_threshold,
                'take_profit_pct': take_profit_pct,
                **score_trades(trades),
            })
            print(f'\r  {ticker}: {i}/{total} combos tested', end='', flush=True)
        print()  # newline after the in-place progress line
        return results

    # Directional: a candle can only ever fire BUY or SELL, never both, so long and short entries
    # can be swept independently rather than jointly (which would square the grid to ~87M
    # combos/ticker). Two sweeps of today's non-vol_len dims, per vol_len, keeps it at 2×.
    dim_combos = list(itertools.product(VOL_MULTIPLIER_RANGE, PRICE_MOVE_PCT_RANGE,
                                         TRAIL_STOP_PCT_RANGE, BODY_RATIO_THRESHOLD_RANGE, TAKE_PROFIT_PCT_RANGE))
    total = len(VOL_LEN_RANGE) * len(dim_combos) * 2
    print(f'{ticker}: directional grid size {total} combos '
          f'({len(VOL_LEN_RANGE)} vol_len × {len(dim_combos)} (vol_multiplier × price_move_pct × '
          f'trail_stop_pct × body_ratio_threshold × take_profit_pct) × 2 directions)')

    disabled_params = {'vol_multiplier': 1.0, 'price_move_pct': 1.0, 'trail_stop_pct': 1.0,
                        'body_ratio_threshold': DISABLED_BODY_RATIO_THRESHOLD, 'take_profit_pct': 1.0}
    results = []
    done = 0
    for vol_len in VOL_LEN_RANGE:
        for direction in ('long', 'short'):
            for vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, take_profit_pct in dim_combos:
                this_params = {
                    'vol_multiplier': vol_multiplier, 'price_move_pct': price_move_pct,
                    'trail_stop_pct': trail_stop_pct, 'body_ratio_threshold': body_ratio_threshold,
                    'take_profit_pct': take_profit_pct,
                }
                long_params = this_params if direction == 'long' else disabled_params
                short_params = this_params if direction == 'short' else disabled_params
                trades, _ = run_backtest(ticker, low_df, high_df, START_DT, TIMEFRAME, vol_len,
                                          0, 0, 0, 0, QUANTITY, 0,
                                          long_params=long_params, short_params=short_params)
                results.append({'direction': direction, 'vol_len': vol_len, **this_params, **score_trades(trades)})
                done += 1
                print(f'\r  {ticker}: {done}/{total} combos tested', end='', flush=True)
    print()
    return results


# Usage: print_results_table('RKLB', results)
def print_results_table(ticker: str, results: list[dict]) -> None:
    """Print the top TOP_N combos for one ticker, ranked by SELECTION_METRIC (results must already
    be sorted). Directional results (mixed long/short rows) get an extra `dir` column."""
    directional = any(r['direction'] != 'shared' for r in results)
    print(f'\n=== {ticker}: top {min(TOP_N, len(results))} of {len(results)} combos, ranked by {SELECTION_METRIC} ===')
    dir_hdr = f"  {'dir':>5}" if directional else ''
    print(f"  {'vol_len':>7}{dir_hdr}  {'vol_mult':>9}  {'price_pct':>10}  {'trail_pct':>10}  {'tp_pct':>7}  {'body_ratio':>11}  "
          f"{'trades':>7}  {'win_rate':>9}  {'total_pnl':>10}  {'expectancy':>11}  {'max_dd':>9}  {'sortino':>8}  {'score':>8}")
    for r in results[:TOP_N]:
        dir_val = f"  {r['direction']:>5}" if directional else ''
        print(f"  {r['vol_len']:>7d}{dir_val}  {r['vol_multiplier']:>9.2f}  {r['price_move_pct']:>10.2f}  {r['trail_stop_pct']:>10.2f}  "
              f"{r['take_profit_pct']:>7.2f}  {r['body_ratio_threshold']:>11.2f}  {r['trade_count']:>7d}  {r['win_rate']:>8.0%}  "
              f"{r['total_pnl']:>+10.2f}  {r['expectancy']:>+11.2f}  {r['max_drawdown']:>9.2f}  {r['sortino']:>8.2f}  {r['combined_score']:>+8.2f}")


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
    """Pick this ticker's best combo(s) by SELECTION_METRIC and write them into
    tuner1_found_paramsN.py, in the same PARAMS[strategy][ticker][timeframe] shape as
    configs_rocketJanek.py — merging with whatever's already there for other tickers.

    Detects directional results from `results` itself (see tune_ticker()): if every row is
    'shared', one flat param set is saved (today's behavior, unchanged). If long/short rows are
    present, the best long combo and best short combo are combined into one params dict with
    _long/_short suffixed keys, sharing whichever vol_len maximizes their summed SELECTION_METRIC
    (vol_len can't differ by direction — see the module comment above TUNE_DIRECTIONAL)."""
    if all(r['direction'] == 'shared' for r in results):
        best = max(results, key=rank_key(SELECTION_METRIC))
        params = {
            'vol_len': best['vol_len'],
            'vol_multiplier': best['vol_multiplier'],
            'price_move_pct': best['price_move_pct'],
            'trail_stop_pct': best['trail_stop_pct'],
            'body_ratio_threshold': best['body_ratio_threshold'],
            'take_profit_pct': best['take_profit_pct'],
        }
        summary = f"{SELECTION_METRIC} {best[SELECTION_METRIC]:+.3f}"
    else:
        long_rows = [r for r in results if r['direction'] == 'long']
        short_rows = [r for r in results if r['direction'] == 'short']
        best_long_by_vl = {vl: max((r for r in long_rows if r['vol_len'] == vl), key=rank_key(SELECTION_METRIC), default=None) for vl in VOL_LEN_RANGE}
        best_short_by_vl = {vl: max((r for r in short_rows if r['vol_len'] == vl), key=rank_key(SELECTION_METRIC), default=None) for vl in VOL_LEN_RANGE}
        valid_vls = [vl for vl in VOL_LEN_RANGE if best_long_by_vl[vl] is not None and best_short_by_vl[vl] is not None]
        best_vol_len = max(valid_vls, key=lambda vl: rank_key(SELECTION_METRIC)(best_long_by_vl[vl]) + rank_key(SELECTION_METRIC)(best_short_by_vl[vl]))
        best_long, best_short = best_long_by_vl[best_vol_len], best_short_by_vl[best_vol_len]
        params = {
            'vol_len': best_vol_len,
            'vol_multiplier_long': best_long['vol_multiplier'], 'vol_multiplier_short': best_short['vol_multiplier'],
            'price_move_pct_long': best_long['price_move_pct'], 'price_move_pct_short': best_short['price_move_pct'],
            'trail_stop_pct_long': best_long['trail_stop_pct'], 'trail_stop_pct_short': best_short['trail_stop_pct'],
            'body_ratio_threshold_long': best_long['body_ratio_threshold'], 'body_ratio_threshold_short': best_short['body_ratio_threshold'],
            'take_profit_pct_long': best_long['take_profit_pct'], 'take_profit_pct_short': best_short['take_profit_pct'],
        }
        summary = (f"{SELECTION_METRIC} long={best_long[SELECTION_METRIC]:+.3f} "
                   f"short={best_short[SELECTION_METRIC]:+.3f} (vol_len={best_vol_len})")

    all_params = _load_found_params()
    all_params.setdefault('MomentumV8Strategy', {}).setdefault(ticker, {})[TIMEFRAME] = params
    _write_found_params(all_params)
    logger.info('Saved best params for %s (%s, %s) to %s: %s', ticker, TIMEFRAME, summary, FOUND_PARAMS_FILE, params)


# Usage: plot_3d('RKLB', results, 'vol_multiplier', 'price_move_pct', 'total_pnl')
# Usage (directional): plot_3d('RKLB', results, 'vol_multiplier', 'price_move_pct', 'total_pnl', direction='long')
def plot_3d(ticker: str, results: list[dict], param_x: str, param_y: str, z_metric: str, direction: str = None) -> None:
    """3D surface of 2 tuned parameters (x, y) against a chosen performance metric (z_metric, any
    of _SCORE_KEYS). Each grid point is the best z_metric found across all values of the other 3
    tuned parameters for that (x, y) combination. Builds the figure but doesn't show it — call
    plt.show() once after plotting every ticker so none of them block in turn.
    Pass direction='long'/'short'/'shared' to filter first — required for directional results
    (mixed long/short rows), so a long-tuned combo and a short-tuned combo aren't mixed onto the
    same surface."""
    if z_metric not in _SCORE_KEYS:
        raise ValueError(f"z_metric must be one of {_SCORE_KEYS}")
    if direction is not None:
        results = [r for r in results if r['direction'] == direction]
    pick = min if z_metric in _LOWER_IS_BETTER else max

    best = {}
    for r in results:
        v = r[z_metric]
        if not np.isfinite(v):  # inf profit_factor / recovery_factor
            continue
        key = (r[param_x], r[param_y])
        best[key] = v if key not in best else pick(best[key], v)

    xs = sorted({x for x, y in best})
    ys = sorted({y for x, y in best})
    X, Y = np.meshgrid(xs, ys)
    Z = np.array([[best.get((x, y), np.nan) for x in xs] for y in ys])

    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(projection='3d')
    surf = ax.plot_surface(X, Y, Z, cmap='RdYlGn')
    ax.set_xlabel(param_x)
    ax.set_ylabel(param_y)
    ax.set_zlabel(z_metric)
    ax.set_title(f'{ticker}: best {z_metric} by {param_x} / {param_y}')
    fig.colorbar(surf, label=z_metric)


# Usage: print_ticker_ranking(results_by_ticker, 'combined_score')
def print_ticker_ranking(results_by_ticker: dict[str, list[dict]], metric: str = SELECTION_METRIC) -> None:
    """Rank tickers by their own best combo's value of `metric` (default SELECTION_METRIC), best
    ticker first. The best combo is independently reselected using `metric` for each ticker,
    so switching metrics always reflects that metric's own best pick, not a stale one. For
    directional results this shows each ticker's single best-performing side (`dir` column) —
    see save_best_params() for how both sides get merged into one saved params dict."""
    best_per_ticker = {ticker: max(results, key=rank_key(metric)) for ticker, results in results_by_ticker.items()}
    ranked = sorted(best_per_ticker.items(), key=lambda kv: rank_key(metric)(kv[1]), reverse=True)
    directional = any(r['direction'] != 'shared' for r in best_per_ticker.values())

    print(f'\n=== Ticker ranking by {metric} (best combo per ticker) ===')
    dir_hdr = f"  {'dir':>5}" if directional else ''
    print(f"  {'#':>3}  {'ticker':6}  {metric:>11}{dir_hdr}  {'vol_len':>7}  {'vol_mult':>9}  {'price_pct':>10}  {'trail_pct':>10}  {'tp_pct':>7}  {'body_ratio':>11}")
    for i, (ticker, r) in enumerate(ranked, 1):
        metric_str = f"{r[metric]:>+11.0%}" if metric == 'win_rate' else f"{r[metric]:>+11.2f}"
        dir_val = f"  {r['direction']:>5}" if directional else ''
        print(f"  {i:>3}  {ticker:6}  {metric_str}{dir_val}  {r['vol_len']:>7d}  {r['vol_multiplier']:>9.2f}  "
              f"{r['price_move_pct']:>10.2f}  {r['trail_stop_pct']:>10.2f}  {r['take_profit_pct']:>7.2f}  {r['body_ratio_threshold']:>11.2f}")


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
        #plot_3d(ticker, results, 'vol_multiplier', 'price_move_pct', 'expectancy')
        if TUNE_DIRECTIONAL:
            plot_3d(ticker, results, 'vol_multiplier', 'trail_stop_pct', 'expectancy', direction='long')
            plot_3d(ticker, results, 'vol_multiplier', 'trail_stop_pct', 'expectancy', direction='short')
        else:
            plot_3d(ticker, results, 'vol_multiplier', 'trail_stop_pct', 'expectancy')

    print_ticker_ranking(results_by_ticker)
    plt.show()  # blocks once, here, after every ticker's figure has been built


if __name__ == '__main__':
    main()
