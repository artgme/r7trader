import csv
import threading
import time
from pathlib import Path

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

HOST = '127.0.0.1'
PORT = 4003
CLIENT_ID = 105
SYMBOL = 'KLAC'
N_BARS = 5
POLL_SECONDS = 120
LOG_FILE = Path('logs/klac_tws_api_log.csv')


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

LOG_FILE.parent.mkdir(exist_ok=True)

# Runs every POLL_SECONDS on one persistent connection (connect once, repeat the request).
# Wide log: one row per poll. Columns are close+volume pairs for iloc[-1]..iloc[-N_BARS],
# newest first. Header (written once) shows the candle time each position had on that first
# poll, just as an orientation example -- later rows hold whatever candle occupies that
# position by then, since positions are relative, not tied to a fixed calendar time.
try:
    while True:
        app.bars = []
        app.done.clear()
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

        last_n = list(reversed(app.bars[-N_BARS:]))  # iloc[-1] first, then iloc[-2], ...
        is_new = not LOG_FILE.exists()
        with LOG_FILE.open('a', newline='') as f:
            writer = csv.writer(f)
            if is_new:
                header = ['request_time']
                for i, b in enumerate(last_n, start=1):
                    header.append(f'iloc[-{i}] close (e.g. {b.date})')
                    header.append(f'iloc[-{i}] volume (e.g. {b.date})')
                writer.writerow(header)

            row = [time.strftime('%H:%M')]
            for b in last_n:
                row.append(b.close)
                row.append(b.volume)
            writer.writerow(row)

        print(f'--- sleeping {POLL_SECONDS}s ---')
        time.sleep(POLL_SECONDS)
except KeyboardInterrupt:
    pass
finally:
    app.disconnect()
