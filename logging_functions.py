import csv
import datetime
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

EXCHANGE_TZ = ZoneInfo('America/New_York')

_CSV_HEADERS = ['timestamp', 'symbol', 'broker', 'action', 'price', 'size', 'position_after']


def init_trade_log(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        with open(log_path, 'w', newline='') as f:
            csv.writer(f).writerow(_CSV_HEADERS)


def log_trade_csv(log_path: Path, action: str, symbol: str, price: float, size: float, position_after: str):
    if not log_path.exists():
        init_trade_log(log_path)
    with open(log_path, 'a', newline='') as f:
        csv.writer(f).writerow([
            datetime.datetime.now(EXCHANGE_TZ).strftime('%Y-%m-%d %H:%M:%S.%f'),
            symbol, 'ibkr', action, price, size, position_after,
        ])
    logger.info('Trade logged: %s %s %s @ %.4f → %s', action, size, symbol, price, position_after)


_SIGNAL_CSV_HEADERS = ['timestamp', 'symbol', 'signal', 'volume', 'mean_volume', 'current_pct',
                       'price_threshold', 'body_ratio', 'green_volume', 'green_price', 'red_price', 'green_body',
                       'price_threshold_long', 'price_threshold_short', 'green_volume_long', 'green_volume_short',
                       'green_body_long', 'green_body_short']
# trail_stop_pct used to live here, but it's no longer a "check" quantity — since the trailing
# stop split by direction, which value actually applies is an execution-time decision made once
# `direction` is known (see rocket_janek.py / run_backtest()), not something check_vol_price_body()
# computes. The price_threshold_*/green_*_long/short columns are blank for a shared-params call
# (check_vol_price_body()) and the plain price_threshold/green_* columns are blank for a
# directional call (check_vol_price_body_dir()) — whichever wasn't used for that call.


def init_signal_log(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        with open(log_path, 'w', newline='') as f:
            csv.writer(f).writerow(_SIGNAL_CSV_HEADERS)


# Usage: log_signal_csv(SIGNAL_LOG, symbol, signal, debug, flags)
def log_signal_csv(log_path: Path, symbol: str, signal: str, debug: dict, flags: list):
    """Accepts either check_vol_price_body()'s shared-params debug/flags (price_threshold, 4
    flags) or check_vol_price_body_dir()'s directional ones (price_threshold_long/short, 6
    flags) — detected from debug's keys — and logs whichever wasn't used for this call as blank."""
    if not log_path.exists():
        init_signal_log(log_path)
    directional = 'price_threshold_long' in debug
    if directional:
        green_volume_long, green_volume_short, green_price, red_price, green_body_long, green_body_short = flags
        price_threshold, green_volume, green_body = '', '', ''
        price_threshold_long, price_threshold_short = debug['price_threshold_long'], debug['price_threshold_short']
    else:
        green_volume, green_price, red_price, green_body = flags
        price_threshold = debug['price_threshold']
        price_threshold_long = price_threshold_short = ''
        green_volume_long = green_volume_short = green_body_long = green_body_short = ''
    with open(log_path, 'a', newline='') as f:
        csv.writer(f).writerow([
            datetime.datetime.now(EXCHANGE_TZ).strftime('%Y-%m-%d %H:%M:%S.%f'),
            symbol, signal or 'none',
            debug['volume'], debug['mean_volume'], debug['current_pct'], price_threshold, debug['body_ratio'],
            green_volume, green_price, red_price, green_body,
            price_threshold_long, price_threshold_short, green_volume_long, green_volume_short,
            green_body_long, green_body_short,
        ])


_LEGACY_TAKE_PROFIT_PCT = 2.0  # backtester_alpaca's flat take-profit default before the sweep was
                                # added — used only as a fallback when reloading an older tuning
                                # log that predates the take_profit_pct column

_TUNING_CSV_HEADERS = ['tuned_at', 'ticker', 'timeframe', 'direction', 'run_start', 'run_end',
                       'vol_len', 'vol_multiplier', 'price_move_pct', 'trail_stop_pct', 'body_ratio_threshold',
                       'take_profit_pct', 'trade_count', 'win_rate', 'total_pnl', 'expectancy',
                       'max_drawdown', 'profit_factor', 'sharpe', 'sortino', 'recovery_factor', 'combined_score']
# 'direction' is 'shared' for a combo tuned with one parameter set covering both directions (today's
# default), or 'long'/'short' for one half of a directional sweep (see tuner1.TUNE_DIRECTIONAL) —
# in both cases the vol_multiplier/price_move_pct/trail_stop_pct/body_ratio_threshold/take_profit_pct
# columns hold whichever direction's values that row actually searched, same column names either way.

# Score columns added after the first sweeps — load_tuning_log() fills them with NaN for any
# older log that predates them (they can't be recomputed without the per-trade data).
_TUNING_SCORE_COLS = ['trade_count', 'win_rate', 'total_pnl', 'expectancy',
                      'max_drawdown', 'profit_factor', 'sharpe', 'sortino', 'recovery_factor', 'combined_score']


def init_tuning_log(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        with open(log_path, 'w', newline='') as f:
            csv.writer(f).writerow(_TUNING_CSV_HEADERS)


# Usage: log_tuning_csv(TUNING_LOG, 'RKLB', '30m', START_DT, END_DAY, results)
def log_tuning_csv(log_path: Path, ticker: str, timeframe: str, run_start, run_end, results: list[dict]):
    """Appends one row per grid combo in `results` (the list of dicts tune_ticker() returns).
    Safe to call once per ticker as each finishes — a run killed partway through still leaves
    every completed ticker's rows on disk."""
    if not log_path.exists():
        init_tuning_log(log_path)
    tuned_at = datetime.datetime.now(EXCHANGE_TZ).strftime('%Y-%m-%d %H:%M:%S.%f')
    with open(log_path, 'a', newline='') as f:
        writer = csv.writer(f)
        for r in results:
            writer.writerow([
                tuned_at, ticker, timeframe, r.get('direction', 'shared'), run_start, run_end,
                r['vol_len'], r['vol_multiplier'], r['price_move_pct'], r['trail_stop_pct'], r['body_ratio_threshold'],
                r['take_profit_pct'], r['trade_count'], r['win_rate'], r['total_pnl'], r['expectancy'],
                r['max_drawdown'], r['profit_factor'], r['sharpe'], r['sortino'], r['recovery_factor'], r['combined_score'],
            ])


# Usage: results_by_ticker = load_tuning_log(Path('tuning_logs/tuning_30m_20260726_1400.csv'))
def load_tuning_log(log_path: Path) -> dict[str, list[dict]]:
    """Reconstructs the results_by_ticker shape tuner1.py builds in-memory, so plot_3d() and
    print_ticker_ranking() work unmodified on a log reloaded in a later session. Score columns
    a log predates (see _TUNING_SCORE_COLS) come back as NaN — they can't be recomputed here."""
    results_by_ticker: dict[str, list[dict]] = {}
    with open(log_path, newline='') as f:
        for row in csv.DictReader(f):
            rec = {
                # older logs predate the long/short directional sweep — every row in them was
                # tuned with one shared parameter set, so 'shared' is the correct fallback, not a guess.
                'direction': row.get('direction') or 'shared',
                'vol_len': int(row['vol_len']),
                'vol_multiplier': float(row['vol_multiplier']),
                'price_move_pct': float(row['price_move_pct']),
                'trail_stop_pct': float(row['trail_stop_pct']),
                'body_ratio_threshold': float(row['body_ratio_threshold']),
                # older logs predate the take_profit_pct sweep -- fall back to backtester_alpaca's
                # flat default, which is what those runs actually used.
                'take_profit_pct': float(row['take_profit_pct']) if row.get('take_profit_pct') else _LEGACY_TAKE_PROFIT_PCT,
            }
            for col in _TUNING_SCORE_COLS:
                val = row.get(col)
                rec[col] = float(val) if val not in (None, '') else float('nan')
            rec['trade_count'] = int(rec['trade_count']) if rec['trade_count'] == rec['trade_count'] else 0  # NaN check
            results_by_ticker.setdefault(row['ticker'], []).append(rec)
    return results_by_ticker


def make_fill_handler(log_path: Path, default_symbol: str):
    def _on_fill(trade, fill):
        symbol = getattr(trade.contract, 'symbol', default_symbol)
        if trade.order is None:
            # Fill for an order this process didn't place (e.g. from another client/session) —
            # no orderType/ocaGroup/orderRef available to classify it, so just skip the trade log.
            logger.warning('Fill for untracked order (orderId=%s, symbol=%s) — skipping trade log.', fill.execution.orderId, symbol)
            return
        order_type = trade.order.orderType
        side       = fill.execution.side    # 'BOT' or 'SLD'
        price      = fill.execution.avgPrice
        size       = fill.execution.shares
        has_oca    = bool(getattr(trade.order, 'ocaGroup', ''))
        is_manual_close = getattr(trade.order, 'orderRef', '') == 'close_position'

        # Entry orders have no OCA group; TP exits do (TRAIL may or may not).
        is_entry = order_type in ('LMT', 'MKT') and not has_oca and not is_manual_close

        if is_manual_close:
            action, position_after = ('exit_long_manual', 'flat') if side == 'SLD' else ('exit_short_manual', 'flat')
        elif is_entry:
            action, position_after = ('enter_long', 'long') if side == 'BOT' else ('enter_short', 'short')
        elif order_type == 'TRAIL':
            action, position_after = ('exit_long_trail', 'flat') if side == 'SLD' else ('exit_short_trail', 'flat')
        else:  # LMT with OCA = take profit
            action, position_after = ('exit_long_tp', 'flat') if side == 'SLD' else ('exit_short_tp', 'flat')

        log_trade_csv(log_path, action, symbol, price, size, position_after)

    return _on_fill
