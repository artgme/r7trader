import time
import pandas as pd
import mplfinance as mpf
from ibkr import IBKRGateway

CHECK_INTERVAL = 10  # seconds between connection checks


def fetch_and_plot(gw: IBKRGateway, symbol: str = 'AAPL', duration: str = '1 D', bar_size: str = '5 mins'):
    contract = gw.make_stock_contract(symbol)
    bars = gw.fetch_historical(contract, duration=duration, bar_size=bar_size)
    if not bars:
        print('No data returned.')
        return

    df = pd.DataFrame([{
        'Date': b.date, 'Open': b.open, 'High': b.high,
        'Low': b.low, 'Close': b.close, 'Volume': b.volume,
    } for b in bars])
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)

    mean_price = df['Close'].mean()
    mean_volume = df['Volume'].mean()

    ap_price  = mpf.make_addplot([mean_price]  * len(df), panel=0, color='blue',  linestyle='--', width=1)
    ap_volume = mpf.make_addplot([mean_volume] * len(df), panel=1, color='orange', linestyle='--', width=1)

    mpf.plot(df, type='candle', volume=True, title='AAPL — last day (5-min)', style='charles',
         figsize=(12, 8),
         addplot=[ap_price, ap_volume])

    #mpf.plot(df, type='candle', volume=True, title=f'{symbol} — {duration} ({bar_size})', style='charles',
    #         figsize=(12, 8))
    return df


def main():
    gw = IBKRGateway()

    if not gw.ensure_connected():
        print('Could not connect to IBKR. Is the Gateway/TWS running?')
        return

    try:
        #1. Fetch and plot historical data for the stock
        df = fetch_and_plot(gw, 'RKLB', '1 D', '5 mins')
        #2. Calculate indicators
        mean_price = df['Close'].mean()
        print(f'Mean closing price for RKLB: {mean_price:.2f}')
        mean_volume = df['Volume'].mean()
        print(f'Mean volume for RKLB: {mean_volume:.2f}')

        # Keep running, periodically verifying the connection is alive.
        print(f'Monitoring connection every {CHECK_INTERVAL}s. Press Ctrl+C to exit.')
        while True:
            time.sleep(CHECK_INTERVAL)
            if not gw.ensure_connected():
                print('Lost connection and could not reconnect. Exiting.')
                break
            print('Connection OK.')
    except KeyboardInterrupt:
        print('Stopped by user.')
    finally:
        gw.disconnect()


if __name__ == '__main__':
    main()
