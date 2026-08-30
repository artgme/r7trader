import datetime
import logging
import time
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.sync_wrapper import TWSSyncWrapper, ResponseTimeout

RED    = '\033[31m'
GREEN  = '\033[32m'
YELLOW = '\033[33m'
BLUE   = '\033[34m'
CYAN   = '\033[36m'
WHITE  = '\033[37m'
RESET  = '\033[0m'

# Basic default settings for IBKR Gateway / TWS
HOST = '127.0.0.1'
PORT = 4003
CLIENT_ID = 78

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Short timeframe key → IBKR barSizeSetting string.
_BAR_SIZE = {
    '1m': '1 min', '5m': '5 mins', '10m': '10 mins', '15m': '15 mins',
    '30m': '30 mins', '45m': '45 mins', '1h': '1 hour', '2h': '2 hours',
    '4h': '4 hours', '1d': '1 day',
}


# Lightweight stand-ins for the ib_insync objects this codebase used to pass around.
# Attribute names match ib_insync's shape on purpose, so callers built against
# BarData/Position/Trade/Fill objects need little or no change.

@dataclass
class Bar:
    date: datetime.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    contract: Contract
    position: float
    avgCost: float
    account: str = ''


@dataclass
class OrderStatus:
    status: str = 'PendingSubmit'
    filled: float = 0.0
    avgFillPrice: float = 0.0


@dataclass
class Trade:
    contract: Contract
    order: Order
    orderStatus: OrderStatus


@dataclass
class Execution:
    orderId: int
    side: str
    shares: float
    avgPrice: float
    execId: str = ''


@dataclass
class Fill:
    execution: Execution
    contract: Contract = None


def _parse_bar_date(date_str: str) -> datetime.datetime:
    """IBKR's historical-bar date string is date-only for daily+ bars ('20260814'),
    or date+time for intraday bars, optionally suffixed with a named IANA timezone
    on TWS API 10.40+ ('20260814 14:00:00 US/Eastern') — handle all three shapes."""
    parts = date_str.split()
    if len(parts) == 1:
        return datetime.datetime.strptime(parts[0], '%Y%m%d')
    naive = datetime.datetime.strptime(f'{parts[0]} {parts[1]}', '%Y%m%d %H:%M:%S')
    if len(parts) == 3:
        return naive.replace(tzinfo=ZoneInfo(parts[2]))
    return naive


def _bar_from_ibapi(b) -> Bar:
    return Bar(date=_parse_bar_date(b.date), open=b.open, high=b.high, low=b.low, close=b.close, volume=float(b.volume))


def _bar_from_realtime(time: int, open_: float, high: float, low: float, close: float, volume) -> Bar:
    """realtimeBar()'s `time` is Unix epoch seconds (UTC) — unlike historical bars, there's no
    exchange-timezone string attached, so callers wanting exchange-local time must convert."""
    date = datetime.datetime.fromtimestamp(time, tz=datetime.timezone.utc)
    return Bar(date=date, open=open_, high=high, low=low, close=close, volume=float(volume))


def _market_order(action: str, quantity: float) -> Order:
    o = Order()
    o.action = action
    o.orderType = 'MKT'
    o.totalQuantity = quantity
    o.transmit = True
    return o


def _limit_order(action: str, quantity: float, limit_price: float) -> Order:
    o = Order()
    o.action = action
    o.orderType = 'LMT'
    o.totalQuantity = quantity
    o.lmtPrice = limit_price
    o.transmit = True
    return o


class _Wrapper(TWSSyncWrapper):
    """TWSSyncWrapper plus a plain callback-list mechanism for the spontaneous,
    not-request/response events (errors, fills, live position/portfolio pushes)
    that callers used to subscribe to directly via ib_insync's Event objects."""

    def __init__(self, timeout=30):
        super().__init__(timeout=timeout)
        self._orders: dict[int, Trade] = {}
        self._error_handlers = []
        self._fill_handlers = []
        self._position_handlers = []
        self._portfolio_handlers = []
        self._realtimebar_handlers = []

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        super().error(reqId, errorTime, errorCode, errorString, advancedOrderRejectJson)
        for cb in self._error_handlers:
            try:
                cb(reqId, errorCode, errorString)
            except Exception:
                logger.exception('error handler raised')

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        super().orderStatus(orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)
        trade = self._orders.get(orderId)
        if trade is not None:
            trade.orderStatus.status = status
            trade.orderStatus.filled = float(filled)
            trade.orderStatus.avgFillPrice = avgFillPrice

    def execDetails(self, reqId, contract, execution):
        super().execDetails(reqId, contract, execution)
        trade = self._orders.get(execution.orderId)
        if trade is None:
            trade = Trade(contract=contract, order=None, orderStatus=OrderStatus())
        fill = Fill(
            execution=Execution(
                orderId=execution.orderId,
                side=execution.side,
                shares=float(execution.shares),
                avgPrice=execution.avgPrice,
                execId=execution.execId,
            ),
            contract=contract,
        )
        for cb in self._fill_handlers:
            try:
                cb(trade, fill)
            except Exception:
                logger.exception('fill handler raised')

    def position(self, account, contract, position, avgCost):
        super().position(account, contract, position, avgCost)
        for cb in self._position_handlers:
            try:
                cb(account, contract, position, avgCost)
            except Exception:
                logger.exception('position handler raised')

    def updatePortfolio(self, contract, position, marketPrice, marketValue, averageCost, unrealizedPNL, realizedPNL, accountName):
        super().updatePortfolio(contract, position, marketPrice, marketValue, averageCost, unrealizedPNL, realizedPNL, accountName)
        for cb in self._portfolio_handlers:
            try:
                cb(contract, position, marketPrice, marketValue, averageCost, unrealizedPNL, realizedPNL, accountName)
            except Exception:
                logger.exception('portfolio handler raised')

    def realtimeBar(self, reqId, time, open_, high, low, close, volume, wap, count):
        super().realtimeBar(reqId, time, open_, high, low, close, volume, wap, count)
        bar = _bar_from_realtime(time, open_, high, low, close, volume)
        for cb in self._realtimebar_handlers:
            try:
                cb(reqId, bar)
            except Exception:
                logger.exception('realtime bar handler raised')


class IBKRGateway:
    def __init__(self, host: str = HOST, port: int = PORT, client_id: int = CLIENT_ID):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.app = _Wrapper(timeout=30)

    # Connects to TWS / IB Gateway if not already connected; returns True on success.
    # connect() is idempotent — safe to call multiple times.
    def connect(self) -> bool:
        if self.app.isConnected():
            return True
        logger.info('Connecting to IBKR Gateway %s:%s clientId=%s', self.host, self.port, self.client_id)
        connected = self.app.connect_and_start(self.host, self.port, self.client_id)
        logger.info('Connected=%s', connected)
        return connected

    # Gracefully closes the socket to TWS / IB Gateway; no-op if already disconnected.
    def disconnect(self) -> None:
        if self.app.isConnected():
            logger.info('Disconnecting from IBKR Gateway')
            self.app.disconnect_and_stop()
            logger.info('Disconnected')

    # Thin wrapper so callers don't need to touch self.app just to check status.
    def is_connected(self) -> bool:
        return self.app.isConnected()

    # Ensures the gateway is connected, retrying up to `retries` times with exponential backoff.
    # Returns True if connected, False if all attempts failed.
    def ensure_connected(self, retries: int = 3, delay: float = 2.0) -> bool:
        if self.app.isConnected():
            return True
        for attempt in range(1, retries + 1):
            logger.info('Reconnect attempt %d/%d', attempt, retries)
            try:
                if self.app.connect_and_start(self.host, self.port, self.client_id):
                    logger.info('Reconnected on attempt %d', attempt)
                    return True
            except Exception as e:
                logger.warning('Attempt %d failed: %s', attempt, e)
            if attempt < retries:
                time.sleep(delay * attempt)
        logger.error('Failed to reconnect after %d attempts', retries)
        return False

    # Registers a callback for connection/request errors: cb(reqId, errorCode, errorString).
    # Replaces the old `gw.ib.errorEvent += cb` pattern.
    def on_error(self, callback) -> None:
        self.app._error_handlers.append(callback)

    # Registers a callback for trade fills: cb(trade, fill), mirroring ib_insync's
    # execDetailsEvent(trade, fill) shape. Replaces `gw.ib.execDetailsEvent += cb`.
    def on_fill(self, callback) -> None:
        self.app._fill_handlers.append(callback)

    # Registers a callback for live position pushes: cb(account, contract, position, avgCost).
    # Replaces `gw.ib.positionEvent += cb`.
    def on_position_update(self, callback) -> None:
        self.app._position_handlers.append(callback)

    # Registers a callback for live portfolio pushes: cb(contract, position, marketPrice,
    # marketValue, averageCost, unrealizedPNL, realizedPNL, accountName).
    # Replaces `gw.ib.updatePortfolioEvent += cb`.
    def on_portfolio_update(self, callback) -> None:
        self.app._portfolio_handlers.append(callback)

    # Registers a callback for live 5-second bars: cb(reqId, bar). Fires on every bar until
    # stop_realtime_bars(reqId) is called for that request.
    def on_realtime_bar(self, callback) -> None:
        self.app._realtimebar_handlers.append(callback)

    # Opens a continuous position-update stream: handlers registered via on_position_update
    # fire on every subsequent position change until stop_position_updates() is called.
    # get_positions() does its own short-lived request/cancel internally and does not need this.
    def start_position_updates(self) -> None:
        self.app.reqPositions()

    def stop_position_updates(self) -> None:
        self.app.cancelPositions()

    # Opens a continuous portfolio-update stream (position, market value, unrealized/realized
    # PNL) for `account_code` (''  = the account TWS is currently logged into). Handlers
    # registered via on_portfolio_update fire on every update until stop_portfolio_updates().
    def start_portfolio_updates(self, account_code: str = '') -> None:
        self.app.reqAccountUpdates(True, account_code)

    def stop_portfolio_updates(self, account_code: str = '') -> None:
        self.app.reqAccountUpdates(False, account_code)

    # Opens a continuous 5-second-bar subscription for `contract`. Handlers registered via
    # on_realtime_bar fire on every subsequent bar until stop_realtime_bars(reqId) is called.
    # Only 5s bars exist (barSize is ignored by IBKR); what_to_show: 'TRADES', 'MIDPOINT', 'BID', or 'ASK'.
    # Each open subscription counts as one Market Data Line, same as a TWS watchlist row.
    # Returns the reqId needed to stop this specific subscription later.
    def start_realtime_bars(self, contract: Contract, what_to_show: str = 'TRADES', use_rth: bool = True) -> int:
        req_id = self.app.get_next_valid_id()
        self.app.reqRealTimeBars(req_id, contract, 5, what_to_show, int(use_rth), [])
        return req_id

    def stop_realtime_bars(self, req_id: int) -> None:
        self.app.cancelRealTimeBars(req_id)

    # Returns an equity Contract. SMART routing lets IBKR choose the best execution venue automatically.
    def make_stock_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD') -> Contract:
        c = Contract()
        c.symbol = symbol
        c.secType = 'STK'
        c.exchange = exchange
        c.currency = currency
        return c

    # Returns a crypto Contract. PAXOS is IBKR's default crypto exchange; pass 'GEMINI' etc. if needed.
    def make_crypto_contract(self, symbol: str, exchange: str = 'PAXOS', currency: str = 'USD') -> Contract:
        c = Contract()
        c.symbol = symbol
        c.secType = 'CRYPTO'
        c.exchange = exchange
        c.currency = currency
        return c

    # Returns a Forex Contract. Symbol must be a 6-char pair like 'EURUSD'; IDEALPRO is IBKR's
    # interbank FX venue and the only one that supports fractional pip pricing.
    def make_forex_contract(self, symbol: str, exchange: str = 'IDEALPRO') -> Contract:
        if len(symbol) != 6:
            raise ValueError('Forex symbol must be 6 characters, e.g. EURUSD')
        c = Contract()
        c.symbol = symbol[:3]
        c.secType = 'CASH'
        c.currency = symbol[3:]
        c.exchange = exchange
        return c

    # Fetches a one-shot historical bar snapshot up to the current moment.
    # duration follows IBKR format: '1 D', '3 M', '1 Y', etc.
    # bar_size accepts short format ('5m', '1h', '1d') or raw IBKR format ('5 mins', '1 hour', '1 day').
    # what_to_show: 'TRADES' for stocks, 'MIDPOINT' or 'BID_ASK' for Forex/crypto.
    # use_rth=False includes pre/post-market and overnight sessions.
    # end_dt: None/'' (default) means "up to now"; pass a datetime (naive or tz-aware) or a
    # pre-formatted 'yyyyMMdd HH:mm:ss [tz]' string to bound the window.
    def fetch_historical(self, contract: Contract, duration: str = '1 D', bar_size: str = '5m', what_to_show: str = 'TRADES', use_rth: bool = False, end_dt=None) -> list:
        symbol = contract.symbol
        bar_size = _BAR_SIZE.get(bar_size, bar_size)
        if isinstance(end_dt, datetime.datetime):
            end_dt = end_dt.strftime('%Y%m%d %H:%M:%S') + (f' {end_dt.tzinfo}' if end_dt.tzinfo else '')
        logger.info('Requesting historical data: %s, duration=%s, bar_size=%s, what_to_show=%s', symbol, duration, bar_size, what_to_show)
        try:
            raw_bars = self.app.get_historical_data(
                contract,
                end_date_time=end_dt or '',
                duration_str=duration,
                bar_size_setting=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
            )
        except ResponseTimeout:
            raw_bars = []
        bars = [_bar_from_ibapi(b) for b in raw_bars]
        if not bars:
            logger.warning('[ibkr.py] No bars returned for %s (duration=%s, bar_size=%s) — possible causes: no trades in window (after-hours), pacing violation, or HMDS inactive.', symbol, duration, bar_size)
        return bars

    # Returns a list of Position objects (contract, position size, avgCost, account) for all open positions.
    # Returns an empty list when the account is flat.
    def get_positions(self) -> list:
        try:
            raw = self.app.get_positions()
        except ResponseTimeout:
            return []
        positions = []
        for account, items in raw.items():
            for item in items:
                positions.append(Position(contract=item['contract'], position=float(item['position']), avgCost=item['avgCost'], account=account))
        return positions

    # Submits `order` on `contract`, tags it with a fresh order id, and tracks it internally
    # so orderStatus/execDetails callbacks can update it in place. Returns the Trade immediately
    # (does not wait for any status).
    def _submit(self, contract: Contract, order: Order) -> Trade:
        order_id = self.app.get_next_valid_id()
        order.orderId = order_id
        trade = Trade(contract=contract, order=order, orderStatus=OrderStatus())
        self.app._orders[order_id] = trade
        self.app.placeOrder(order_id, contract, order)
        return trade

    # Polls trade.orderStatus.status (updated live by the orderStatus callback) until it reaches
    # a terminal state or `timeout` elapses. Returns the final status string.
    def _wait_for_status(self, trade: Trade, timeout: float, poll: float = 0.5) -> str:
        deadline = time.time() + timeout
        while trade.orderStatus.status not in {'Filled', 'Cancelled', 'Inactive'} and time.time() < deadline:
            time.sleep(poll)
        return trade.orderStatus.status

    # Requests every open order across all clients on the account (not just this one) — this is
    # what catches stale orders left behind by a previous, possibly-crashed session.
    def _get_all_open_orders(self, timeout: float = 5.0) -> dict:
        self.app.open_orders = {}
        self.app.reqAllOpenOrders()
        try:
            return self.app._wait_for_response(0, 'open_orders', timeout) or {}
        except ResponseTimeout:
            logger.warning('Timed out waiting for open orders')
            return {}

    # Cancels resting open orders for `symbol`, optionally filtered by action/orderType.
    # Used to clear stale entry orders before placing a new one, and to clear protective
    # exit orders (trail/TP) before manually closing a position.
    def _cancel_resting_orders(self, symbol: str, action: str = None, order_types=None) -> list:
        open_orders = self._get_all_open_orders()
        matches = [
            (oid, o['contract'], o['order'])
            for oid, o in open_orders.items()
            if getattr(o['contract'], 'symbol', None) == symbol
            and (action is None or o['order'].action == action)
            and (order_types is None or o['order'].orderType in order_types)
        ]
        for oid, _, o in matches:
            self.app.cancelOrder(oid, OrderCancel())
            logger.info(f'{YELLOW}Cancelled %s %s order %s{RESET}', symbol, o.orderType, oid)
        if matches:
            time.sleep(1)
        return matches

    # Places an entry order, waits for it to fill, then attaches a trailing stop.
    # Avoids bracket/transmit=False orders, which IBKR paper trading silently discards.
    # limit_price=None uses a market order; a numeric value uses a limit order.
    # When take_profit_price is set, TP and trail are linked via OCA so the first exit cancels the other.
    # fill_timeout: seconds to wait for the entry to fill before giving up.
    # Returns (entry_trade, take_profit_trade_or_None, trailing_stop_trade_or_None).
    def place_bracket_trailing(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        trail_percent: float = None,
        trail_amount: float = None,
        limit_price: float = None,
        take_profit_price: float = None,
        fill_timeout: float = 60.0,
    ):
        if trail_percent is None and trail_amount is None:
            raise ValueError(f'{RED}Provide either trail_percent or trail_amount{RESET}')
        if trail_percent is not None and trail_amount is not None:
            raise ValueError(f'{RED}Provide only one of trail_percent or trail_amount{RESET}')

        action = action.upper()
        exit_action = 'SELL' if action == 'BUY' else 'BUY'
        symbol = contract.symbol

        # Step 0: cancel stale entry orders from a previous session — never touch protective
        # exit orders (TRAIL/STP), which may be guarding an existing position.
        self._cancel_resting_orders(symbol, action=action, order_types=('LMT', 'MKT'))

        # Step 1: place entry (transmit=True — no bracket, no transmit=False)
        order = _limit_order(action, quantity, limit_price) if limit_price is not None else _market_order(action, quantity)
        placed_entry = self._submit(contract, order)
        logger.info(
            f'{GREEN}Entry placed: %s %s %s @ %s{RESET}',
            action, quantity, symbol,
            f'{limit_price:.4f}' if limit_price is not None else 'MARKET',
        )

        # Step 2: wait for fill
        status = self._wait_for_status(placed_entry, fill_timeout)
        logger.info(f'{GREEN}Entry status: %s{RESET}', status)
        if status != 'Filled':
            logger.warning(f'{YELLOW}Entry not filled after {fill_timeout}s (status={status}) — cancelling order{RESET}')
            self.app.cancelOrder(placed_entry.order.orderId, OrderCancel())
            time.sleep(1)
            return placed_entry, None, None

        filled_qty = placed_entry.orderStatus.filled

        # Step 3: place trail (and optional TP) as standalone orders
        placed_tp = None
        oca_group = None
        if take_profit_price is not None:
            oca_group = f'OCA-{uuid.uuid4().hex[:8]}'
            tp = _limit_order(exit_action, filled_qty, take_profit_price)
            tp.ocaGroup = oca_group
            tp.ocaType = 1
            placed_tp = self._submit(contract, tp)
            logger.info(f'{GREEN}TP placed: %s @ %.4f{RESET}', exit_action, take_profit_price)

        trail = Order()
        trail.action = exit_action
        trail.orderType = 'TRAIL'
        trail.totalQuantity = filled_qty
        trail.transmit = True
        if oca_group:
            trail.ocaGroup = oca_group
            trail.ocaType = 1
        if trail_percent is not None:
            trail.trailingPercent = trail_percent
        else:
            trail.auxPrice = trail_amount
        placed_trail = self._submit(contract, trail)

        time.sleep(1)
        logger.info(
            f'{GREEN}Trail placed: %s | trail=%s | status=%s{RESET}',
            exit_action,
            f'{trail_percent}%' if trail_percent is not None else f'${trail_amount}',
            placed_trail.orderStatus.status,
        )

        return placed_entry, placed_tp, placed_trail

    # Submits a market order and waits 1 second for IBKR to echo back the initial order status.
    # Returns a Trade object whose .orderStatus.status reflects the current state
    # (e.g. 'PreSubmitted', 'Submitted', 'Filled'). For async fills, register a handler via
    # gw.on_fill(...) or poll trade.orderStatus after the fact.
    def place_market_order(self, contract: Contract, action: str, quantity: float) -> Trade:
        action = action.upper()
        if action not in {'BUY', 'SELL'}:
            raise ValueError('Order action must be BUY or SELL')

        order = _market_order(action, quantity)
        logger.info('Placing market order: %s %s %s', action, quantity, contract.symbol)
        trade = self._submit(contract, order)
        time.sleep(1)  # wait for order state update
        logger.info('Order status: %s', trade.orderStatus.status)
        return trade

    # Flattens an existing position for `contract`: looks up its direction/quantity via
    # get_positions(), cancels any resting exit orders for that symbol (the trail stop / take-profit
    # left over from place_bracket_trailing — otherwise they could still fire after a manual close),
    # then submits a single opposite-side order and waits for it to fill. Works for both long and short.
    # limit_price=None submits a market order; a numeric value submits a limit order.
    # quantity=None closes the full position; pass a smaller value for a partial close.
    # fill_timeout: seconds to wait for the close to fill before giving up (the position is then
    # left exactly as IBKR reports it — check trade.orderStatus.status; it will not be 'Filled').
    # Raises ValueError if there's no open position for the contract's symbol.
    def close_position(self, contract: Contract, limit_price: float = None, quantity: float = None, fill_timeout: float = 30.0) -> Trade:
        symbol = contract.symbol
        pos = next((p for p in self.get_positions() if getattr(p.contract, 'symbol', None) == symbol and p.position != 0), None)
        if pos is None:
            raise ValueError(f'{RED}No open position for {symbol}{RESET}')

        close_qty = abs(quantity) if quantity is not None else abs(pos.position)
        action = 'SELL' if pos.position > 0 else 'BUY'

        # Cancel resting exit orders (trail stop / take-profit) so they don't fire after we've closed.
        self._cancel_resting_orders(symbol)

        # Route through SMART rather than reusing `contract` as-is: a Position's contract reflects
        # the specific exchange it's actually settled on (e.g. NYSE/NASDAQ), and placing an order
        # directly on that pinned exchange trips IBKR's "direct routed order" precaution (error
        # 10311) and gets silently cancelled unless that's been allowed in Global Configuration.
        close_contract = self.make_stock_contract(symbol, currency=contract.currency)
        order = _limit_order(action, close_qty, limit_price) if limit_price is not None else _market_order(action, close_qty)
        order.orderRef = 'close_position'  # lets make_fill_handler recognize this as an exit, not an entry
        trade = self._submit(close_contract, order)

        status = self._wait_for_status(trade, fill_timeout)
        color = GREEN if status == 'Filled' else RED
        log = logger.info if status == 'Filled' else logger.error
        log(
            f'{color}Close order: %s %s %s @ %s | status=%s{RESET}',
            action, close_qty, symbol,
            f'{limit_price:.4f}' if limit_price is not None else 'MARKET',
            status,
        )
        return trade


if __name__ == '__main__':
    gateway = IBKRGateway(client_id=78)
    if gateway.connect():
        positions = gateway.get_positions()
        if not positions:
            logger.info('No open positions')
        else:
            logger.info('Open positions (%d):', len(positions))
            for p in positions:
                logger.info('  %s  qty=%.4f  avgCost=%.4f', p.contract.localSymbol or p.contract.symbol, p.position, p.avgCost)
        gateway.disconnect()
