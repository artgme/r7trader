import csv
import time
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ibkr import IBKRGateway

CLIENT_ID = 85
SYMBOL = 'KLAC'
CURRENCY = 'USD'
POLL_SECONDS = 1800  # 30 minutes
EXCHANGE_TZ = ZoneInfo('America/New_York')
LOG_FILE = Path('logs/bar_lag_diagnostic.csv')

# Read-only: polls a live (endDateTime='') historical-data fetch every POLL_SECONDS, prints and
# logs the last 3 bars with the wall-clock time of each request. Run this straddling a 30m
# boundary (e.g. 9:55-10:10 ET) to see, live, whether the just-closed bar shows up as complete
# right away or lags — and whether a partial/forming bar is present at all. Does not place orders.
def main():
    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        print('Could not connect to IBKR.')
        return
    contract = gw.make_stock_contract(SYMBOL, currency=CURRENCY)

    LOG_FILE.parent.mkdir(exist_ok=True)
    is_new = not LOG_FILE.exists()
    log_file = LOG_FILE.open('a', newline='')
    writer = csv.writer(log_file)
    if is_new:
        writer.writerow(['wall_clock', 'bar_date', 'open', 'high', 'low', 'close', 'volume'])

    try:
        while True:
            if not gw.ensure_connected():
                print('Lost connection, will retry next cycle.')
                time.sleep(POLL_SECONDS)
                continue
            now = datetime.datetime.now(EXCHANGE_TZ)
            bars = gw.fetch_historical(contract, duration='21600 S', bar_size='30m', use_rth=True)
            print(f'--- wall clock {now.strftime("%H:%M:%S")} ---')
            for b in bars[-3:]:
                print(' ', b.date, b.open, b.high, b.low, b.close, b.volume)
                writer.writerow([now.isoformat(), b.date, b.open, b.high, b.low, b.close, b.volume])
            log_file.flush()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        log_file.close()
        gw.disconnect()

if __name__ == '__main__':
    main()
