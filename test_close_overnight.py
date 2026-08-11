import time
import datetime

from ibkr import IBKRGateway
import positions_observer as po
from rocket_janek import get_exchange_closing_time, close_all_positions, CLOSE_BEFORE_SECONDS, EXCHANGE_TZ

CLIENT_ID = 98

# Live test of the CLOSE_OVERNIGHT trigger: reuses the real get_exchange_closing_time /
# close_all_positions from rocket_janek.py (not a reimplementation), but skips the 54-symbol
# entry-scanning loop entirely. Waits for the real 10-minutes-to-close window and fires once.
def main():
    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        print('Could not connect to IBKR.')
        return
    po.sync_with_ibkr(gw.get_positions())

    closed_overnight_on = None
    try:
        while True:
            gw.ib.sleep(20)
            now = time.time()
            closing_time = get_exchange_closing_time(now)
            today = datetime.datetime.fromtimestamp(now, tz=EXCHANGE_TZ).date()
            seconds_to_trigger = (closing_time - CLOSE_BEFORE_SECONDS) - now
            print(f'{datetime.datetime.now(EXCHANGE_TZ).strftime("%H:%M:%S")}  '
                  f'{seconds_to_trigger:+.0f}s to trigger window')

            if closed_overnight_on != today and closing_time - CLOSE_BEFORE_SECONDS <= now < closing_time:
                print('>>> Triggering close_all_positions() now <<<')
                close_all_positions(gw)
                closed_overnight_on = today
                print('Done. Final positions:')
                for p in gw.get_positions():
                    if p.position != 0:
                        print(f'  STILL OPEN: {p.contract.symbol} {p.position}')
                break
    except KeyboardInterrupt:
        pass
    finally:
        gw.disconnect()

if __name__ == '__main__':
    main()
