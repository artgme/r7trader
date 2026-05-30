import logging
from ib_insync import IB, Stock, Forex, Crypto, MarketOrder, Contract

# Basic default settings for IBKR Gateway / TWS
HOST = '127.0.0.1'
PORT = 4003
CLIENT_ID = 78

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


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
        logger.info('Requesting historical data: %s, duration=%s, bar_size=%s, what_to_show=%s', contract.symbol if hasattr(contract, 'symbol') else contract.secType, duration, bar_size, what_to_show)
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
        return bars

    # Same API as fetch_historical but with keepUpToDate=True: IBKR pushes new bars as they close.
    # Attach an event handler via bars.updateEvent += handler to receive live updates.
    # Call ib.cancelHistoricalData(bars) when the subscription is no longer needed.
    def fetch_live_bars(self, contract: Contract, duration: str = '1 D', bar_size: str = '1 min', what_to_show: str = 'TRADES', use_rth: bool = False):
        logger.info('Subscribing to live bars: %s, bar_size=%s', contract.symbol if hasattr(contract, 'symbol') else contract.secType, bar_size)
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
        return bars

    # Returns a list of Position objects (contract, position size, avgCost) for all open positions.
    # Returns an empty list when the account is flat.
    def get_positions(self) -> list:
        return self.ib.positions()

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
