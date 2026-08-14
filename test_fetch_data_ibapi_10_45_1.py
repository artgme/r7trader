import csv
import time
from pathlib import Path

from ibapi.sync_wrapper import TWSSyncWrapper
from ibapi.contract import Contract

HOST = '127.0.0.1'
PORT = 4003
CLIENT_ID = 106
SYMBOL = 'KLAC'
N_BARS = 5
POLL_SECONDS = 120
LOG_FILE = Path('logs/klac_ibapi_10_45_1_log_konto1.csv')

contract = Contract()
contract.symbol = SYMBOL
contract.secType = 'STK'
contract.exchange = 'SMART'
contract.currency = 'USD'

# Same idea as test_fetch_data_tws_api.py, but using IBKR's official sync-wrapper
# (ibapi 10.45.1) instead of hand-rolled EWrapper callbacks + threading.Event.
app = TWSSyncWrapper(timeout=30)
app.connect_and_start(HOST, PORT, CLIENT_ID)

LOG_FILE.parent.mkdir(exist_ok=True)

# Runs every POLL_SECONDS on one persistent connection. Same wide log layout as before:
# one row per poll, close+volume pairs for iloc[-1]..iloc[-N_BARS], newest first. Header
# (written once) shows the candle time each position had on that first poll, as an
# orientation example -- positions are relative, not tied to a fixed calendar time.
try:
    while True:
        bars = app.get_historical_data(
            contract,
            end_date_time='',
            duration_str=f'{N_BARS * 1800} S',
            bar_size_setting='30 mins',
            what_to_show='TRADES',
            use_rth=True,
        )

        for b in bars:
            print(b.date, b.open, b.high, b.low, b.close, b.volume)

        last_n = list(reversed(bars[-N_BARS:]))  # iloc[-1] first, then iloc[-2], ...
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
    app.disconnect_and_stop()
