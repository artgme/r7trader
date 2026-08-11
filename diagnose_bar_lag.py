import csv
import time
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ibkr import IBKRGateway

CLIENT_ID = 85
SYMBOL = 'EURUSD'  # Forex trades ~24/5, so every 30m boundary is testable right now
N_BARS = 5
POLL_SECONDS = 900  # 15 minutes
EXCHANGE_TZ = ZoneInfo('America/New_York')
LOG_FILE = Path('logs/bar_lag_forex.csv')


def slot_label(i: int) -> str:
    if i == 0:
        return 'oldest'
    if i == N_BARS - 1:
        return 'newest'
    return f'newest-{N_BARS - 1 - i}'


# Read-only: every POLL_SECONDS, fetches a live (endDateTime='') 30m-bar snapshot and writes ONE
# ROW per poll with the last N_BARS bars laid out side by side, oldest -> newest. Scan down any
# one column and you can see directly whether that slot's candle actually changes every 30 min
# (correct) or sits stuck at the same bar_time for many rows in a row (the bug). No orders placed.
def main():
    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        print('Could not connect to IBKR.')
        return
    contract = gw.make_forex_contract(SYMBOL)

    LOG_FILE.parent.mkdir(exist_ok=True)
    is_new = not LOG_FILE.exists()
    log_file = LOG_FILE.open('a', newline='')
    writer = csv.writer(log_file)
    if is_new:
        header = ['wall_clock']
        header += [f'bar_time({slot_label(i)})' for i in range(N_BARS)]
        for i in range(N_BARS):
            header.append(f'price_close({slot_label(i)})')
            header.append(f'volume({slot_label(i)})')
        writer.writerow(header)

    try:
        while True:
            if not gw.ensure_connected():
                print('Lost connection, will retry.')
                time.sleep(POLL_SECONDS)
                continue

            now = datetime.datetime.now(EXCHANGE_TZ)
            bars = gw.fetch_historical(contract, duration='16200 S', bar_size='30m', what_to_show='MIDPOINT', use_rth=False)
            last_n = bars[-N_BARS:]

            row = [now.isoformat()]
            row += [b.date.strftime('%H:%M:%S') for b in last_n]
            for b in last_n:
                row += [b.close, b.volume]

            print(now.strftime('%H:%M:%S'), '  ', [f"{b.date.strftime('%H:%M')}={b.close}" for b in last_n])
            writer.writerow(row)
            log_file.flush()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        log_file.close()
        gw.disconnect()

if __name__ == '__main__':
    main()
