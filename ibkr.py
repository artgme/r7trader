import logging
from ib_insync import IB, Stock, Forex, MarketOrder, Contract

# Basic default settings for IBKR Gateway / TWS
HOST = '127.0.0.1'
PORT = 7497
CLIENT_ID = 1

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


class IBKRGateway:
    def __init__(self, host: str = HOST, port: int = PORT, client_id: int = CLIENT_ID):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()

    def connect(self) -> bool:
        if not self.ib.isConnected():
            logger.info('Connecting to IBKR Gateway %s:%s clientId=%s', self.host, self.port, self.client_id)
            self.ib.connect(self.host, self.port, clientId=self.client_id)
        connected = self.ib.isConnected()
        logger.info('Connected=%s', connected)
        return connected

    def disconnect(self) -> None:
        if self.ib.isConnected():
            logger.info('Disconnecting from IBKR Gateway')
            self.ib.disconnect()
            logger.info('Disconnected')

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    def make_stock_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD') -> Contract:
        return Stock(symbol, exchange=exchange, currency=currency)

    def make_forex_contract(self, symbol: str, exchange: str = 'IDEALPRO', currency: str = 'USD') -> Contract:
        if len(symbol) != 6:
            raise ValueError('Forex symbol must be 6 characters, e.g. EURUSD')
        base_currency = symbol[:3]
        quote_currency = symbol[3:]
        return Forex(base_currency, quote_currency=quote_currency, exchange=exchange)

    def fetch_historical(self, contract: Contract, duration: str = '1 D', bar_size: str = '5 mins', what_to_show: str = 'TRADES', use_rth: bool = False):
        logger.info('Requesting historical data: %s, duration=%s, bar_size=%s, what_to_show=%s', contract.symbol if hasattr(contract, 'symbol') else contract.secType, duration, bar_size, what_to_show)
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime='',
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=False,
        )
        return bars

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
    gateway = IBKRGateway()
    if gateway.connect():
        contract = gateway.make_stock_contract('AAPL')
        bars = gateway.fetch_historical(contract, duration='1 D', bar_size='15 mins')
        logger.info('Received %d bars', len(bars))
        gateway.disconnect()
