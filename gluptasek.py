import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ib_insync').setLevel(logging.WARNING)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway
import configs_rocketJanek as cfg
import time
from logging_functions import init_trade_log, make_fill_handler

CLIENT_ID=78

CHECK_INTERVAL = 100  # sekundy pomiędzy sprawdzeniem połączenia
SYMBOL = 'RKLB' #ASM, BESI - EUR
TIMEFRAME = '10m'
QUANTITY = 100


TRADE_LOG = Path('logs/trades_rklb_gluptasek_26Jun2.csv')

RED    = '\033[31m'
GREEN  = '\033[32m'
YELLOW = '\033[33m'
BLUE   = '\033[34m'
CYAN   = '\033[36m'
WHITE  = '\033[37m'
RESET  = '\033[0m'

def round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)

def timeframe_to_seconds(tf: str) -> int:
    if tf.endswith('m'):
        return int(tf[:-1]) * 60
    if tf.endswith('h'):
        return int(tf[:-1]) * 3600
    raise ValueError(f'Unsupported timeframe: {tf}')

def fetch_data_from_IBKR(gw: IBKRGateway, symbol: str = 'RKLB', duration: str = '1 D', bar_size: str = '5m', use_rth: bool = False, currency: str = 'USD'):
    contract = gw.make_stock_contract(symbol, currency=currency)
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
    gw = IBKRGateway(client_id=CLIENT_ID)
    logging.info("Connecting to IBKR...")

    if not gw.ensure_connected():
        logger.error(f'{RED}Could not connect to IBKR. Is the Gateway/TWS running?{RESET}')
        return
    def _on_ibkr_error(reqId, code, msg, _):
        # codes >= 2000 are connection/system info; 202 = order cancelled confirmation
        if code >= 2000 or code == 202:
            logger.debug(f'IBKR info {code} (reqId={reqId}): {msg}')
        else:
            logger.error(f'{RED}IBKR error {code} (reqId={reqId}): {msg}{RESET}')
    gw.ib.errorEvent += _on_ibkr_error
    #logging data
    init_trade_log(TRADE_LOG)
    gw.ib.execDetailsEvent += make_fill_handler(TRADE_LOG, SYMBOL)

    def _on_fill(trade, fill):
        logger.info(
            f'FILL: {fill.execution.side} {fill.execution.shares} {trade.contract.symbol} '
            f'@ {fill.execution.avgPrice:.4f} | orderId={fill.execution.orderId}'
        )

    gw.ib.execDetailsEvent += _on_fill

    try:
        #1. Pobiera paramtry strategii z configs.py:
        params     = cfg.get_params('MomentumV8Strategy', SYMBOL, TIMEFRAME)
        pd.set_option('display.max_rows', None)
        logger.debug(f'Parameters for {SYMBOL}: {params}')

        currency_par = params.get('currency', 'USD')
        vol_len = params.get('vol_len', 10)

        vol_multiplier = params.get('vol_multiplier', 1.5)
        price_move_pct = params.get('price_move_pct', 1.0)
        trail_stop_pct = params.get('trail_stop_pct', 0.2)
        tick_size = params.get('tick_size', 0.01)

        #2. Calculating timings for fetching data:
        tf_seconds = timeframe_to_seconds(TIMEFRAME)
        fetch_interval = tf_seconds                        # fetch once per bar
        duration = f'{vol_len * tf_seconds} S'             # enough bars to fill vol_len

        #2. Pobierz dane historyczne z IBKR
        df = fetch_data_from_IBKR(gw, SYMBOL, '1 D', TIMEFRAME, use_rth=True, currency=currency_par)
        if df is None:
            logger.error('No initial data — market may be closed or pacing violation. Exiting.')
            return

        #2. Inicjalizacja - oblicz indicators
        mean_price = df['Close'].mean()
        logger.debug(f'Mean closing price for {SYMBOL}: {mean_price:.2f}')
        mean_volume = df['Volume'].mean()
        logger.debug(f'Mean volume for {SYMBOL}: {mean_volume:.2f}')

        #3. Wyświetl dane na wykresie
        fig, axes = plot_candles_and_mean(df, mean_price, mean_volume)
        plt.pause(0.5)  # let the window render before entering the loop

        # Keep running, periodically verifying the connection is alive.
        logger.debug(f'Monitoruję połączenie co {CHECK_INTERVAL} [s]. Wciśnij Ctrl+C aby zakończyć działanie programu.')
        last_fetch = 0
        last_processed_candle = None
        contract = gw.make_stock_contract(SYMBOL, currency=currency_par)
        while True:
            # Interleave ib.sleep (asyncio) and plt.pause (GUI) so both stay responsive.
            for _ in range(CHECK_INTERVAL):
                gw.ib.sleep(0.9)
                plt.pause(0.1)
            if not gw.ensure_connected():
                logger.error('Lost connection and could not reconnect. Exiting.')
                break
            #logger.debug('...')
            now = time.time()
            #logger.debug(f'tick: now={now:.0f}, last_fetch={last_fetch:.0f}, diff={now - last_fetch:.0f}s')
            #Zbuduj równanie
            if now - last_fetch >= fetch_interval: #5minut * 60 = 300s; 10min *60 =600s
                #4. Download life data
                # W zaleności od ilości świeczek do analizy, pobieramy dane historyczne z IBKR. W tym przypadku pobieramy 20 świeczek po 5 minut każda, co daje nam 100 minut danych (7200 sekund).
                df = fetch_data_from_IBKR(gw, SYMBOL, duration, TIMEFRAME, use_rth=True, currency=currency_par) #12 x 10 min = 120 min <= 7200 s
                df = df.tail(vol_len)
                last_candle = df.iloc[-2]
                candle_time = last_candle.name  # DatetimeIndex — unique per candle

                if candle_time == last_processed_candle:
                    logger.debug(f'{YELLOW}Candle {candle_time} already processed, skipping.{RESET}')
                else:
                    price = last_candle['Close'] #use in stop loss and take profit orders
                    volume = last_candle['Volume']
                    #5. Update indicators
                    df['candle_pct'] = 100 * (df['Close'] - df['Open']) / df['Open']
                    #print(df['candle_pct'])
                    current_pct = df['candle_pct'].iloc[-2]
                    mean_abs_change = df['candle_pct'].abs().mean()
                    mean_abs_change_sl = df['candle_pct'].abs().iloc[:-2].mean()
                    trail_stop_loss = max(mean_abs_change_sl * trail_stop_pct, 0.1)
                    logger.info(f"{YELLOW}Trail stop loss: {trail_stop_loss:.2f}% {RESET}")
                    #print(f'Mean absolute change: {mean_abs_change:.2f}%')
                    mean_volume = df['Volume'].mean()
                    #7. Check for signals and execute orders
                    volume_threshold = mean_volume * vol_multiplier
                    price_threshold = mean_abs_change * price_move_pct
                    logger.info(f'{YELLOW}volume: {volume:.2f}, mean_volume: {mean_volume:.2f}, current_pct: {current_pct:.2f}, price_threshold: {price_threshold:.2f}{RESET}')

                    if volume > volume_threshold and current_pct > price_threshold:
                        logger.info(f'{GREEN}BUY: candle_pct {current_pct:.2f}% > {price_threshold:.2f}% | volume {volume:.0f} > {volume_threshold:.0f}{RESET}')
                        SIGNAL = 'BUY'
                    elif volume > volume_threshold and current_pct < -price_threshold:
                        logger.info(f'{RED}SELL: candle_pct {current_pct:.2f}% < -{price_threshold:.2f}% | volume {volume:.0f} > {volume_threshold:.0f}{RESET}')
                        SIGNAL = 'SELL'
                    else:
                        SIGNAL = None

                    positions = gw.get_positions()
                    already_in_position = any(getattr(p.contract, 'symbol', None) == SYMBOL for p in positions)
                    if SIGNAL and already_in_position:
                        logger.info(f'{YELLOW}Signal {SIGNAL} skipped — already in position.{RESET}')
                    elif SIGNAL == 'SELL':
                        entry, tp, trail = gw.place_bracket_trailing(
                            contract,
                            action='SELL',
                            quantity=QUANTITY,
                            limit_price=round_to_tick(price * 0.995, tick_size),
                            trail_percent=trail_stop_loss,
                        )
                    elif SIGNAL == 'BUY':
                        entry, tp, trail = gw.place_bracket_trailing(
                            contract,
                            action='BUY',
                            quantity=QUANTITY,
                            limit_price=round_to_tick(price * 1.005, tick_size),
                            trail_percent=trail_stop_loss,
                        )
                    
                    last_processed_candle = candle_time
                #printing positions
                positions = gw.get_positions()
                if positions:
                    for p in positions:
                        logger.debug(f'{BLUE}Position: {p}{RESET}')
                else:
                    logger.debug(f'{YELLOW}No open positions.{RESET}')
                last_fetch = now
                #logger.debug(f'Next fetch in 300s at {time.strftime("%H:%M:%S", time.localtime(last_fetch + 300))}')

    except KeyboardInterrupt:
        logger.info('Stopped by user.')
    finally:
        gw.disconnect()


if __name__ == '__main__':
    main()
