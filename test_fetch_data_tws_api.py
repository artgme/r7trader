import threading
import time

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

HOST = '127.0.0.1'
PORT = 4003
CLIENT_ID = 105
SYMBOL = 'KLAC'
N_BARS = 10


# Minimal native TWS API client (not ib_insync) -- to check whether the same bar-lag
# behavior shows up when talking to IBKR directly, bypassing ib_insync entirely.
class App(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.bars = []
        self.done = threading.Event()

    def historicalData(self, reqId, bar):
        self.bars.append(bar)

    def historicalDataEnd(self, reqId, start, end):
        self.done.set()

    def error(self, reqId, errorCode, errorString):
        if errorCode < 2100:  # codes >= 2100 are informational (farm/connection status)
            print(f'ERROR {errorCode} (reqId={reqId}): {errorString}')


contract = Contract()
contract.symbol = SYMBOL
contract.secType = 'STK'
contract.exchange = 'SMART'
contract.currency = 'USD'

app = App()
app.connect(HOST, PORT, CLIENT_ID)

thread = threading.Thread(target=app.run, daemon=True)
thread.start()
time.sleep(1)  # let the connection handshake finish before requesting data

app.reqHistoricalData(
    reqId=1,
    contract=contract,
    endDateTime='',
    durationStr=f'{N_BARS * 1800} S',
    barSizeSetting='30 mins',
    whatToShow='TRADES',
    useRTH=1,
    formatDate=1,
    keepUpToDate=False,
    chartOptions=[],
)

app.done.wait(timeout=30)

for b in app.bars:
    print(b.date, b.open, b.high, b.low, b.close, b.volume)

app.disconnect()
