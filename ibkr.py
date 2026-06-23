import logging
import time
import uuid
from ib_insync import IB, Stock, Forex, Crypto, MarketOrder, LimitOrder, Order, Contract

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


class _IBKRWrapperFilter(logging.Filter):
    """Drop high-frequency account-wide broadcasts from ib_insync.wrapper.
    mozg.py subscribes to these events and logs only the relevant symbol."""
    def filter(self, record):
        msg = record.getMessage()
        return not msg.startswith(('updatePortfolio:', 'position:'))

logging.getLogger('ib_insync.wrapper').addFilter(_IBKRWrapperFilter())


class IBKRGateway:
    def __init__(self, host: str = HOST, port: int = PORT, client_id: int = CLIENT_ID):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()

    # Connects to TWS / IB Gateway if not already connected; returns True on success.
    # connect() is idempotent — safe to call multiple times.
    def connect(self) -> bool:
        if not self.ib.isConnected():
            logger.info('Connecting to IBKR Gateway %s:%s clientId=%s', self.host, self.port, self.client_id)
            self.ib.connect(self.host, self.port, clientId=self.client_id)
        connected = self.ib.isConnected()
        logger.info('Connected=%s', connected)
        return connected

    # Gracefully closes the socket to TWS / IB Gateway; no-op if already disconnected.
    def disconnect(self) -> None:
        if self.ib.isConnected():
            logger.info('Disconnecting from IBKR Gateway')
            self.ib.disconnect()
            logger.info('Disconnected')

    # Thin wrapper so callers don't need to import ib_insync directly just to check status.
    def is_connected(self) -> bool:
        return self.ib.isConnected()

    # Ensures the gateway is connected, retrying up to `retries` times with exponential backoff.
    # Returns True if connected, False if all attempts failed.
    def ensure_connected(self, retries: int = 3, delay: float = 2.0) -> bool:
        if self.ib.isConnected():
            return True
        for attempt in range(1, retries + 1):
            logger.info('Reconnect attempt %d/%d', attempt, retries)
            try:
                self.ib.connect(self.host, self.port, clientId=self.client_id)
                if self.ib.isConnected():
                    logger.info('Reconnected on attempt %d', attempt)
                    return True
            except Exception as e:
                logger.warning('Attempt %d failed: %s', attempt, e)
            if attempt < retries:
                time.sleep(delay * attempt)
        logger.error('Failed to reconnect after %d attempts', retries)
        return False

    # Returns an equity Contract. SMART routing lets IBKR choose the best execution venue automatically.
    def make_stock_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD') -> Contract:
        return Stock(symbol, exchange=exchange, currency=currency)

    # Returns a crypto Contract. PAXOS is IBKR's default crypto exchange; pass 'GEMINI' etc. if needed.
    def make_crypto_contract(self, symbol: str, exchange: str = 'PAXOS', currency: str = 'USD') -> Contract:
        return Crypto(symbol, exchange=exchange, currency=currency)

    # Returns a Forex Contract. Symbol must be a 6-char pair like 'EURUSD'; IDEALPRO is IBKR's
    # interbank FX venue and the only one that supports fractional pip pricing.
    def make_forex_contract(self, symbol: str, exchange: str = 'IDEALPRO') -> Contract:
        if len(symbol) != 6:
            raise ValueError('Forex symbol must be 6 characters, e.g. EURUSD')
        return Forex(symbol, exchange=exchange)

    # Fetches a one-shot historical bar snapshot up to the current moment (keepUpToDate=False).
    # duration follows IBKR format: '1 D', '3 M', '1 Y', etc.
    # bar_size accepts short format ('5m', '1h', '1d') or raw IBKR format ('5 mins', '1 hour', '1 day').
    # what_to_show: 'TRADES' for stocks, 'MIDPOINT' or 'BID_ASK' for Forex/crypto.
    # use_rth=False includes pre/post-market and overnight sessions.
    def fetch_historical(self, contract: Contract, duration: str = '1 D', bar_size: str = '5m', what_to_show: str = 'TRADES', use_rth: bool = False):
        symbol = contract.symbol if hasattr(contract, 'symbol') else contract.secType
        bar_size = _BAR_SIZE.get(bar_size, bar_size)
        logger.info('Requesting historical data: %s, duration=%s, bar_size=%s, what_to_show=%s', symbol, duration, bar_size, what_to_show)
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime='',  # '' means "up to now"
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=False,  # one-shot; no streaming updates
        )
        if not bars:
            logger.warning('[ibkr.py] No bars returned for %s (duration=%s, bar_size=%s) — possible causes: no trades in window (after-hours), pacing violation, or HMDS inactive.', symbol, duration, bar_size)
        return bars

    # Same API as fetch_historical but with keepUpToDate=True: IBKR pushes new bars as they close.
    # Attach an event handler via bars.updateEvent += handler to receive live updates.
    # Call ib.cancelHistoricalData(bars) when the subscription is no longer needed.
    def fetch_live_bars(self, contract: Contract, duration: str = '1 D', bar_size: str = '1m', what_to_show: str = 'TRADES', use_rth: bool = False):
        symbol = contract.symbol if hasattr(contract, 'symbol') else contract.secType
        bar_size = _BAR_SIZE.get(bar_size, bar_size)
        logger.info('Subscribing to live bars: %s, bar_size=%s', symbol, bar_size)
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime='',
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=True,  # stream new bars as they close
        )
        if not bars:
            logger.warning('[ibkr.py] Live bar subscription returned no initial data for %s — timed out. Will still receive bars via updateEvent once IBKR starts pushing them.', symbol)
        return bars

    # Returns a list of Position objects (contract, position size, avgCost) for all open positions.
    # Returns an empty list when the account is flat.
    def get_positions(self) -> list:
        return self.ib.positions()

    # Places an entry order, waits for it to fill, then attaches a trailing stop.
    # Avoids bracket/transmit=False orders, which IBKR paper trading silently discards.
    # limit_price=None uses a MarketOrder; a numeric value uses a LimitOrder.
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
        fill_timeout: float = 30.0,
    ):
        if trail_percent is None and trail_amount is None:
            raise ValueError(f'{RED}Provide either trail_percent or trail_amount{RESET}')
        if trail_percent is not None and trail_amount is not None:
            raise ValueError(f'{RED}Provide only one of trail_percent or trail_amount{RESET}')

        action = action.upper()
        exit_action = 'SELL' if action == 'BUY' else 'BUY'
        symbol = contract.symbol if hasattr(contract, 'symbol') else contract.secType

        # Step 0: sync open orders from TWS (catches stale orders from previous sessions).
        # Cancel only stale entry orders (LMT/MKT in the entry direction) — never touch
        # protective exit orders (TRAIL/STP) which may be guarding an existing position.
        self.ib.reqAllOpenOrders()
        self.ib.sleep(1)
        stale = [
            t for t in self.ib.openTrades()
            if getattr(t.contract, 'symbol', None) == symbol
            and t.order.action == action
            and t.order.orderType in ('LMT', 'MKT')
            and t.orderStatus.status not in ('Filled', 'Cancelled', 'Inactive')
        ]
        for t in stale:
            self.ib.cancelOrder(t.order)
            logger.warning(f'{YELLOW}Cancelled stale {symbol} {t.order.orderType} order {t.order.orderId}{RESET}')
        if stale:
            self.ib.sleep(1)

        # Step 1: place entry (transmit=True — no bracket, no transmit=False)
        if limit_price is not None:
            entry = LimitOrder(action, quantity, limit_price)
        else:
            entry = MarketOrder(action, quantity)
        placed_entry = self.ib.placeOrder(contract, entry)
        logger.info(
            f'{GREEN}Entry placed: %s %s %s @ %s{RESET}',
            action, quantity, symbol,
            f'{limit_price:.4f}' if limit_price is not None else 'MARKET',
        )

        # Step 2: wait for fill
        deadline = time.time() + fill_timeout
        while placed_entry.orderStatus.status not in {'Filled', 'Cancelled', 'Inactive'} \
                and time.time() < deadline:
            self.ib.sleep(0.5)

        status = placed_entry.orderStatus.status
        logger.info(f'{GREEN}Entry status: %s{RESET}', status)
        if status != 'Filled':
            logger.warning(f'{YELLOW}Entry not filled after {fill_timeout}s (status={status}) — skipping trail{RESET}')
            return placed_entry, None, None

        filled_qty = placed_entry.orderStatus.filled

        # Step 3: place trail (and optional TP) as standalone orders
        placed_tp = None
        oca_group = None
        if take_profit_price is not None:
            oca_group = f'OCA-{uuid.uuid4().hex[:8]}'
            tp = LimitOrder(exit_action, filled_qty, take_profit_price)
            tp.ocaGroup = oca_group
            tp.ocaType = 1
            placed_tp = self.ib.placeOrder(contract, tp)
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
        placed_trail = self.ib.placeOrder(contract, trail)

        self.ib.sleep(1)
        logger.info(
            f'{GREEN}Trail placed: %s | trail=%s | status=%s{RESET}',
            exit_action,
            f'{trail_percent}%' if trail_percent is not None else f'${trail_amount}',
            placed_trail.orderStatus.status,
        )

        return placed_entry, placed_tp, placed_trail

    # Submits a market order and waits 1 second for IBKR to echo back the initial order status.
    # Returns a Trade object whose .orderStatus.status reflects the current state
    # (e.g. 'PreSubmitted', 'Submitted', 'Filled').  For async fills, attach a handler to
    # trade.fillEvent or poll trade.orderStatus after the fact.
    def place_market_order(self, contract: Contract, action: str, quantity: float):
        action = action.upper()
        if action not in {'BUY', 'SELL'}:
            raise ValueError('Order action must be BUY or SELL')

        order = MarketOrder(action, quantity)
        logger.info('Placing market order: %s %s %s', action, quantity, contract.symbol if hasattr(contract, 'symbol') else contract.secType)
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)  # wait for order state update
        logger.info('Order status: %s', trade.orderStatus.status)
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
