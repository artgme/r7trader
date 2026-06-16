import logging
import uuid
from ib_insync import IB, Stock, Forex, Crypto, MarketOrder, LimitOrder, Order, Contract

# Basic default settings for IBKR Gateway / TWS
HOST = '127.0.0.1'
PORT = 4003
CLIENT_ID = 78

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


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
    # bar_size follows IBKR format: '1 min', '5 mins', '1 hour', '1 day', etc.
    # what_to_show: 'TRADES' for stocks, 'MIDPOINT' or 'BID_ASK' for Forex/crypto.
    # use_rth=False includes pre/post-market and overnight sessions.
    def fetch_historical(self, contract: Contract, duration: str = '1 D', bar_size: str = '5 mins', what_to_show: str = 'TRADES', use_rth: bool = False):
        symbol = contract.symbol if hasattr(contract, 'symbol') else contract.secType
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
    def fetch_live_bars(self, contract: Contract, duration: str = '1 D', bar_size: str = '1 min', what_to_show: str = 'TRADES', use_rth: bool = False):
        symbol = contract.symbol if hasattr(contract, 'symbol') else contract.secType
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

    # Places a market or limit entry with a trailing stop and an optional take-profit limit.
    # limit_price=None uses a MarketOrder; a numeric value uses a LimitOrder.
    # When both TP and trail are present they are linked via an OCA group so the first
    # exit to trigger cancels the other.
    # trail_percent: trail by %, e.g. 2.0 means 2%.  Pass trail_amount instead for a fixed $ trail.
    # Returns (entry_trade, take_profit_trade_or_None, trailing_stop_trade).
    def place_bracket_trailing(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        trail_percent: float = None,
        trail_amount: float = None,
        limit_price: float = None,
        take_profit_price: float = None,
    ):
        if trail_percent is None and trail_amount is None:
            raise ValueError('Provide either trail_percent or trail_amount')
        if trail_percent is not None and trail_amount is not None:
            raise ValueError('Provide only one of trail_percent or trail_amount')

        action = action.upper()
        exit_action = 'SELL' if action == 'BUY' else 'BUY'

        if limit_price is not None:
            entry = LimitOrder(action, quantity, limit_price, transmit=False)
        else:
            entry = MarketOrder(action, quantity, transmit=False)
        placed_entry = self.ib.placeOrder(contract, entry)
        parent_id = placed_entry.order.orderId

        placed_tp = None
        if take_profit_price is not None:
            oca_group = f'OCA-{uuid.uuid4().hex[:8]}'
            tp = LimitOrder(exit_action, quantity, take_profit_price, transmit=False)
            tp.parentId = parent_id
            tp.ocaGroup = oca_group
            tp.ocaType = 1
        else:
            oca_group = None

        trail = Order()
        trail.action = exit_action
        trail.orderType = 'TRAIL'
        trail.totalQuantity = quantity
        trail.parentId = parent_id
        trail.transmit = True
        if oca_group:
            trail.ocaGroup = oca_group
            trail.ocaType = 1
        if trail_percent is not None:
            trail.trailingPercent = trail_percent
        else:
            trail.auxPrice = trail_amount

        logger.info(
            'Placing bracket: %s %s %s @ %s | TP=%s | trail=%s',
            action, quantity, contract.symbol if hasattr(contract, 'symbol') else contract.secType,
            f'{limit_price:.4f}' if limit_price is not None else 'MARKET',
            f'{take_profit_price:.4f}' if take_profit_price is not None else 'none',
            f'{trail_percent}%' if trail_percent is not None else f'${trail_amount}',
        )

        if take_profit_price is not None:
            placed_tp = self.ib.placeOrder(contract, tp)
        placed_trail = self.ib.placeOrder(contract, trail)
        self.ib.sleep(1)

        logger.info('Entry status: %s', placed_entry.orderStatus.status)
        if placed_tp:
            logger.info('TP status: %s', placed_tp.orderStatus.status)
        logger.info('Trail status: %s', placed_trail.orderStatus.status)

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
