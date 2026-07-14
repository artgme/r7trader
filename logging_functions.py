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


def make_fill_handler(log_path: Path, default_symbol: str):
    def _on_fill(trade, fill):
        symbol     = getattr(trade.contract, 'symbol', default_symbol)
        order_type = trade.order.orderType
        side       = fill.execution.side    # 'BOT' or 'SLD'
        price      = fill.execution.avgPrice
        size       = fill.execution.shares
        has_oca    = bool(getattr(trade.order, 'ocaGroup', ''))

        # Entry orders have no OCA group; TP exits do (TRAIL may or may not).
        is_entry = order_type in ('LMT', 'MKT') and not has_oca

        if is_entry:
            action, position_after = ('enter_long', 'long') if side == 'BOT' else ('enter_short', 'short')
        elif order_type == 'TRAIL':
            action, position_after = ('exit_long_trail', 'flat') if side == 'SLD' else ('exit_short_trail', 'flat')
        else:  # LMT with OCA = take profit
            action, position_after = ('exit_long_tp', 'flat') if side == 'SLD' else ('exit_short_tp', 'flat')

        log_trade_csv(log_path, action, symbol, price, size, position_after)

    return _on_fill
