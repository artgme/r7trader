import csv
import datetime
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            symbol, 'ibkr', action, price, size, position_after,
        ])
    logger.info('Trade logged: %s %s %s @ %.4f → %s', action, size, symbol, price, position_after)


def make_fill_handler(log_path: Path, default_symbol: str):
    def _on_fill(trade, fill, _):
        symbol     = getattr(trade.contract, 'symbol', default_symbol)
        order_type = trade.order.orderType  # 'LMT', 'MKT', 'TRAIL'
        side       = fill.execution.side    # 'BOT' or 'SLD'
        price      = fill.execution.avgPrice
        size       = fill.execution.shares

        if side == 'BOT':
            action, position_after = 'enter_long', 'long'
        elif order_type == 'TRAIL':
            action, position_after = 'exit_long_trail', 'flat'
        elif order_type == 'LMT':
            action, position_after = 'exit_long_tp', 'flat'
        else:
            action, position_after = 'exit_long', 'flat'

        log_trade_csv(log_path, action, symbol, price, size, position_after)

    return _on_fill
