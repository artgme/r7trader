import threading
import time

from ibapi.client import *
from ibapi.wrapper import *
from ibapi.ticktype import TickTypeEnum

HOST = '127.0.0.1'
PORT = 4003  # IB Gateway paper account, matching every other script today
CLIENT_ID = 107
SYMBOL = 'KLAC'


class TestApp(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)

    def nextValidId(self, orderId: OrderId):
        self.orderId = orderId

    def nextId(self):
        self.orderId += 1
        return self.orderId

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderReject=""):
        print(f"reqId: {reqId}, errorCode: {errorCode}, errorString: {errorString}, orderReject: {advancedOrderReject}", flush=True)

    def tickPrice(self, reqId, tickType, price, attrib):
        print(f"reqId: {reqId}, tickType: {TickTypeEnum.toStr(tickType)}, price: {price}, attrib: {attrib}", flush=True)

    def tickSize(self, reqId, tickType, size):
        print(f"reqId: {reqId}, tickType: {TickTypeEnum.toStr(tickType)}, size: {size}", flush=True)


app = TestApp()
app.connect(HOST, PORT, CLIENT_ID)
thread = threading.Thread(target=app.run, daemon=True)
thread.start()
time.sleep(1)

contract = Contract()
contract.symbol = SYMBOL
contract.secType = 'STK'
contract.exchange = 'SMART'
contract.currency = 'USD'

app.reqMarketDataType(1)  # 1 = live (error 10089 if not entitled, no auto-fallback) -- 3 = delayed, always works
app.reqMktData(app.nextId(), contract, "", False, False, [])

print(f'Streaming {SYMBOL}... Ctrl+C to stop.')
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    app.disconnect()
