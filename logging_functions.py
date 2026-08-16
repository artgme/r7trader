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
                       'price_threshold', 'trail_stop_pct', 'body_ratio', 'green_volume', 'green_price',
                       'red_price', 'green_body']


def init_signal_log(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        with open(log_path, 'w', newline='') as f:
            csv.writer(f).writerow(_SIGNAL_CSV_HEADERS)


# Usage: log_signal_csv(SIGNAL_LOG, symbol, signal, trail_stop_loss, debug, flags)
def log_signal_csv(log_path: Path, symbol: str, signal: str, trail_stop_loss: float, debug: dict, flags: list):
    if not log_path.exists():
        init_signal_log(log_path)
    green_volume, green_price, red_price, green_body = flags
    with open(log_path, 'a', newline='') as f:
        csv.writer(f).writerow([
            datetime.datetime.now(EXCHANGE_TZ).strftime('%Y-%m-%d %H:%M:%S.%f'),
            symbol, signal or 'none',
            debug['volume'], debug['mean_volume'], debug['current_pct'], debug['price_threshold'], trail_stop_loss,
            debug['body_ratio'], green_volume, green_price, red_price, green_body,
        ])


_TUNING_CSV_HEADERS = ['tuned_at', 'ticker', 'timeframe', 'run_start', 'run_end',
                       'vol_len', 'vol_multiplier', 'price_move_pct', 'trail_stop_pct', 'body_ratio_threshold',
                       'trade_count', 'win_rate', 'total_pnl', 'expectancy']


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
                tuned_at, ticker, timeframe, run_start, run_end,
                r['vol_len'], r['vol_multiplier'], r['price_move_pct'], r['trail_stop_pct'], r['body_ratio_threshold'],
                r['trade_count'], r['win_rate'], r['total_pnl'], r['expectancy'],
            ])


# Usage: results_by_ticker = load_tuning_log(Path('tuning_logs/tuning_30m_20260726_1400.csv'))
def load_tuning_log(log_path: Path) -> dict[str, list[dict]]:
    """Reconstructs the results_by_ticker shape tuner1.py builds in-memory, so plot_3d() and
    print_ticker_ranking() work unmodified on a log reloaded in a later session."""
    results_by_ticker: dict[str, list[dict]] = {}
    with open(log_path, newline='') as f:
        for row in csv.DictReader(f):
            results_by_ticker.setdefault(row['ticker'], []).append({
                'vol_len': int(row['vol_len']),
                'vol_multiplier': float(row['vol_multiplier']),
                'price_move_pct': float(row['price_move_pct']),
                'trail_stop_pct': float(row['trail_stop_pct']),
                'body_ratio_threshold': float(row['body_ratio_threshold']),
                'trade_count': int(row['trade_count']),
                'win_rate': float(row['win_rate']),
                'total_pnl': float(row['total_pnl']),
                'expectancy': float(row['expectancy']),
            })
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
