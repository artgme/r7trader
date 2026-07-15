import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('rocket_janek').setLevel(logging.WARNING)  # buy_or_sell() logs INFO per call — way too noisy across a grid sweep

import datetime
import itertools
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient

from backtester_alpaca import fetch_range, run_backtest, ALPACA_API_KEY, ALPACA_SECRET_KEY
import configs_rocketJanek as cfg

TICKERS = ['RKLB', 'QNT', 'AAPL']  # tuned one at a time, results reported per ticker
TIMEFRAME = '30m'
START_DT = datetime.datetime(2026, 7, 1, 9, 30, tzinfo=ZoneInfo('America/New_York'))
END_DAY = datetime.date(2026, 7, 14)
QUANTITY = 10
TOP_N = 10  # how many best combos to print per ticker

# Grid to search — coarse for now, narrow in once a promising region shows up.
VOL_MULTIPLIER_RANGE = [1.2, 1.5, 1.8, 2.0]
PRICE_MOVE_PCT_RANGE = [1.0, 1.5, 2.0]
TRAIL_STOP_PCT_RANGE = [0.5, 1.0, 1.5, 2.0]
BODY_RATIO_THRESHOLD_RANGE = [0.3, 0.5, 0.7]


# Usage: score = score_trades(trades)
def score_trades(trades: list[dict]) -> dict:
    """Summarize one backtest run's trades. Total P&L alone can reward a combo that just
    caught one lucky trade — trade_count and win_rate come along so that can be spotted."""
    closed = [t for t in trades if t['pnl'] is not None]
    total_pnl = sum(t['pnl'] for t in closed)
    wins = sum(1 for t in closed if t['pnl'] > 0)
    win_rate = wins / len(closed) if closed else 0.0
    return {'total_pnl': total_pnl, 'trade_count': len(closed), 'win_rate': win_rate}


# Usage: results = tune_ticker(ticker, low_df, high_df, vol_len)
def tune_ticker(ticker: str, low_df, high_df, vol_len: int) -> list[dict]:
    """Grid-search every parameter combination for one ticker against already-fetched data —
    fetching happens once in main(), run_backtest() itself makes no network calls."""
    combos = list(itertools.product(VOL_MULTIPLIER_RANGE, PRICE_MOVE_PCT_RANGE,
                                     TRAIL_STOP_PCT_RANGE, BODY_RATIO_THRESHOLD_RANGE))
    total = len(combos)
    print(f'{ticker}: grid size {total} combos '
          f'({len(VOL_MULTIPLIER_RANGE)} vol_multiplier × {len(PRICE_MOVE_PCT_RANGE)} price_move_pct × '
          f'{len(TRAIL_STOP_PCT_RANGE)} trail_stop_pct × {len(BODY_RATIO_THRESHOLD_RANGE)} body_ratio_threshold)')

    results = []
    for i, (vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold) in enumerate(combos, 1):
        trades, _ = run_backtest(ticker, low_df, high_df, START_DT, TIMEFRAME, vol_len,
                                  vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, QUANTITY)
        results.append({
            'vol_multiplier': vol_multiplier,
            'price_move_pct': price_move_pct,
            'trail_stop_pct': trail_stop_pct,
            'body_ratio_threshold': body_ratio_threshold,
            **score_trades(trades),
        })
        print(f'\r  {ticker}: {i}/{total} combos tested', end='', flush=True)
    print()  # newline after the in-place progress line
    return results


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.error('Set ALPACA_API_KEY and ALPACA_API_SECRET in .env before running this.')
        return
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    # Same vol_len source as backtester_alpaca.py/rocket_janek.py — not part of the grid yet.
    params = cfg.get_params('MomentumV8Strategy', 'RKLB', TIMEFRAME)
    vol_len = params.get('vol_len', 10)

    fetch_start_day = START_DT.date() - datetime.timedelta(days=5)  # extra lookback so vol_len has history

    for ticker in TICKERS:
        low_df = fetch_range(client, ticker, fetch_start_day, END_DAY, TIMEFRAME)
        high_df = fetch_range(client, ticker, fetch_start_day, END_DAY, '1m')
        if low_df.empty or high_df.empty:
            logger.warning('No data for %s — skipping.', ticker)
            continue

        results = tune_ticker(ticker, low_df, high_df, vol_len)
        results.sort(key=lambda r: r['total_pnl'], reverse=True)

        print(f'\n=== {ticker}: top {min(TOP_N, len(results))} of {len(results)} combos, ranked by total P&L ===')
        print(f"  {'vol_mult':>9}  {'price_pct':>10}  {'trail_pct':>10}  {'body_ratio':>11}  {'trades':>7}  {'win_rate':>9}  {'total_pnl':>10}")
        for r in results[:TOP_N]:
            print(f"  {r['vol_multiplier']:>9.2f}  {r['price_move_pct']:>10.2f}  {r['trail_stop_pct']:>10.2f}  "
                  f"{r['body_ratio_threshold']:>11.2f}  {r['trade_count']:>7d}  {r['win_rate']:>8.0%}  {r['total_pnl']:>+10.2f}")


if __name__ == '__main__':
    main()
