import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ib_insync').setLevel(logging.WARNING)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway
import configs as cfg
import time

CHECK_INTERVAL = 10  # sekundy pomiędzy sprawdzeniem połączenia
SYMBOL = 'RKLB'

def fetch_data_from_IBKR(gw: IBKRGateway, symbol: str = 'RKLB', duration: str = '1 D', bar_size: str = '5m', use_rth: bool = False):
    contract = gw.make_stock_contract(symbol)
    bars = gw.fetch_historical(contract, duration=duration, bar_size=bar_size, use_rth=use_rth)
    if not bars:
        logger.error('No data returned.')
        return

    df = pd.DataFrame([{
        'Date': b.date, 'Open': b.open, 'High': b.high,
        'Low': b.low, 'Close': b.close, 'Volume': b.volume,
    } for b in bars])
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)

    return df

def plot_candles_and_mean(df: pd.DataFrame, mean_price: float, mean_volume: float):
    ap_price  = mpf.make_addplot([mean_price]  * len(df), panel=0, color='blue',  linestyle='--', width=1)
    ap_volume = mpf.make_addplot([mean_volume] * len(df), panel=1, color='orange', linestyle='--', width=1)

    fig, axes = mpf.plot(df, type='candle', volume=True, title='Data', style='charles',
         figsize=(12, 8),
         addplot=[ap_price, ap_volume],
         returnfig=True)
    plt.show(block=False)
    return fig, axes


def main():
    gw = IBKRGateway()
    logging.info("Connecting to IBKR...")

    if not gw.ensure_connected():
        logger.error('Could not connect to IBKR. Is the Gateway/TWS running?')
        return

    try:
        #1. Pobiera paramtry strategii z configs.py:
        params     = cfg.get_params('MomentumV8Strategy', SYMBOL, '10m')
        logger.debug(f'Parameters for RKLB: {params}')
        vol_multiplier = params.get('vol_multiplier', 1.0)
        price_move_pct = params.get('price_move_pct', 1.0)

        #2. Pobierz dane historyczne z IBKR
        df = fetch_data_from_IBKR(gw, SYMBOL, '1 D', '5m', use_rth=True)
        if df is None:
            logger.error('No initial data — market may be closed or pacing violation. Exiting.')
            return

        #2. Inicjalizacja - oblicz indicators
        mean_price = df['Close'].mean()
        logger.debug(f'Mean closing price for RKLB: {mean_price:.2f}')
        mean_volume = df['Volume'].mean()
        logger.debug(f'Mean volume for RKLB: {mean_volume:.2f}')

        #3. Wyświetl dane na wykresie
        fig, axes = plot_candles_and_mean(df, mean_price, mean_volume)

        # Keep running, periodically verifying the connection is alive.
        logger.debug(f'Monitoruję połączenie co {CHECK_INTERVAL} [s]. Wciśnij Ctrl+C aby zakończyć działanie programu.')
        last_fetch = 0
        while True:
            plt.pause(CHECK_INTERVAL)
            if not gw.ensure_connected():
                logger.error('Lost connection and could not reconnect. Exiting.')
                break
            #logger.debug('...')
            now = time.time()
            #logger.debug(f'tick: now={now:.0f}, last_fetch={last_fetch:.0f}, diff={now - last_fetch:.0f}s')
            if now - last_fetch >= 300:
                #4. Download life data
                df = fetch_data_from_IBKR(gw, SYMBOL, '7200 S', '5m', use_rth=True) #20 x 5 min = 100 min <= 7200 s
                df = df.tail(20)
                last_candle = df.iloc[-2]
                price = last_candle['Close']
                volume = last_candle['Volume']
                #5. Update indicators
                mean_price = df['Close'].mean()
                mean_volume = df['Volume'].mean()
                logger.debug(f'Last candle - Price: {price:.2f}, Mean Price: {mean_price:.2f}, Last candle - Volume: {volume:.2f}, Mean Volume: {mean_volume:.2f}')
                #6. Update plot
                #7. Check for signals and execute orders
                volume_threshold = mean_volume * vol_multiplier
                price_threshold = mean_price * price_move_pct
                #logger.debug(f'Volume threshold: {volume_threshold:.2f}')
                #Limit order testing:
                contract = gw.make_stock_contract(SYMBOL)
                entry, tp, trail = gw.place_bracket_trailing(
                    contract,
                    action='SELL',#BUY or SELL
                    quantity=1,
                    limit_price=price,       # None = market entry
                    trail_percent=0.5,       # or trail_amount=1.0 for fixed $
                )
                #printing positions
                positions = gw.get_positions()
                if positions:
                    for p in positions:
                        logger.debug(f'Position: {p}')
                else:
                    logger.debug('No open positions.')
                last_fetch = now
                #logger.debug(f'Next fetch in 300s at {time.strftime("%H:%M:%S", time.localtime(last_fetch + 300))}')

    except KeyboardInterrupt:
        logger.info('Stopped by user.')
    finally:
        gw.disconnect()


if __name__ == '__main__':
    main()
