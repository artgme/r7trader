import pandas as pd

from ibkr import IBKRGateway
from rocket_janek import fetch_data_from_IBKR

CLIENT_ID = 99
SYMBOL = 'SATL'
CURRENCY = 'USD'
DURATION = '4800 S' #80minutes => 8 bars of 10 minutes
BAR_SIZE = '10m'
USE_RTH = True


def main():
    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        print('Could not connect to IBKR.')
        return

    pd.set_option('display.max_rows', None)
    df = fetch_data_from_IBKR(gw, SYMBOL, DURATION, BAR_SIZE, use_rth=USE_RTH, currency=CURRENCY)
    print(df)
    print(f"iloc[-1]: {df.iloc[-1]}")

    gw.disconnect()


if __name__ == '__main__':
    main()
