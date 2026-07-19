import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ib_insync').setLevel(logging.WARNING)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

import datetime
import importlib
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway
import params_lookup
import time
from logging_functions import init_trade_log, make_fill_handler, init_signal_log, log_signal_csv, EXCHANGE_TZ

CLIENT_ID=79

CONFIG_MODULE = 'tuner1_found_params2'  # swap to e.g. 'tuner1_found_params' to trade tuner-found params instead

CHECK_INTERVAL = 100  # sekundy pomiędzy sprawdzeniem połączenia
SYMBOLS = [
    'AMAT', 'LITE', 'ALAB', 'STX', 'CIEN', 'AMD', 'MPWR', 'SIMO', 'BE', 'ISRG',
    'CRDO', 'ENTG', 'MXL', 'NBIS', 'KLAC', 'AMKR', 'DELL', 'AEHR', 'MRVL', 'HOOD',
    'RCL', 'ARM', 'NXT', 'INTC', 'APTV', 'APO', 'UAL', 'ASTS', 'ARWR', 'GTLB',
    'VSAT', 'QNT', 'VSH', 'TEAM', 'REZI', 'LEN', 'BROS', 'ALK', 'AOSL', 'CSCO',
    'DHI', 'A', 'MRNA', 'AFRM', 'MWH', 'CEVA', 'RIOT', 'CVNA', 'IREN', 'HPE',
    'DAL', 'RBLX', 'IVZ', 'JOBY',
]
#SYMBOLS = ['AIXA','BESI','SOI','ASML','SIE.DE','IFX','MC.PA','AMS']
SYMBOL_CURRENCY = {symbol: 'USD' for symbol in SYMBOLS}
#SYMBOL_CURRENCY = {'AIXA':'EUR','BESI':'EUR','SOI':'EUR','ASML':'EUR','SIE.DE':'EUR','IFX':'EUR','MC.PA':'EUR','AMS':'EUR'}
TIMEFRAME = '30m'
QUANTITY = 10
FILL_TIMEOUT = 10
LIVE_TRADING = True
EXCHANGE_OPEN_TIME = datetime.time(9, 30)

LOG_SUFFIX = f"{datetime.datetime.now(EXCHANGE_TZ).strftime('%Y%m%d_%H%M')}_{TIMEFRAME}"
TRADE_LOG = Path(f'logs/trades_{LOG_SUFFIX}.csv')
SIGNAL_LOG = Path(f'logs/signals_{LOG_SUFFIX}.csv')

RED    = '\033[31m'
GREEN  = '\033[32m'
YELLOW = '\033[33m'
BLUE   = '\033[34m'
CYAN   = '\033[36m'
WHITE  = '\033[37m'
RESET  = '\033[0m'

def execute_trade(gw: IBKRGateway, symbol: str, signal: str, contract, quantity: int, trail_stop_loss: float, fill_timeout: float, positions: list):
    if not signal:
        return
    already_in_position = any(getattr(p.contract, 'symbol', None) == symbol for p in positions)

    if already_in_position:
        logger.info(f'{YELLOW}Signal {signal} skipped — already in position.{RESET}')
    elif signal == 'SELL':
        entry, tp, trail = gw.place_bracket_trailing(
            contract,
            action='SELL',
            quantity=quantity,
            trail_percent=trail_stop_loss,
            fill_timeout=fill_timeout,
        )
    elif signal == 'BUY':
        entry, tp, trail = gw.place_bracket_trailing(
            contract,
            action='BUY',
            quantity=quantity,
            trail_percent=trail_stop_loss,
            fill_timeout=fill_timeout,
        )

def buy_or_sell(df: pd.DataFrame, vol_multiplier: float, price_move_pct: float, trail_stop_pct: float, body_ratio_threshold: float) -> tuple:
    #1. Aktualne dane
    last_candle = df.iloc[-2]
    price = last_candle['Close'] #use in stop loss and take profit orders
    volume = last_candle['Volume']

    #2. Zaktualizuj indykatory
    #df['candle_pct'] = 100 * (df['Close'] - df['Open']) / df['Open']
    df['candle_pct'] = 100 * (df['Close'] - df['Open']) / df['Open'].replace(0, float('nan'))
    #print(df['candle_pct'])
    current_pct = df['candle_pct'].iloc[-2]
    mean_abs_change = df['candle_pct'].abs().iloc[:-2].mean()
    #trail_stop_loss = max(mean_abs_change * trail_stop_pct, 0.1)
    trail_stop_loss = min(max(mean_abs_change * trail_stop_pct, 0.4), 3.0)
    logger.info(f"{YELLOW}Trail stop loss: {trail_stop_loss:.2f}% {RESET}")
    #print(f'Mean absolute change: {mean_abs_change:.2f}%')
    mean_volume = df['Volume'].mean()
    
    volume_threshold = mean_volume * vol_multiplier
    price_threshold = mean_abs_change * price_move_pct

    # Body-to-range ratio: 1.0 = pure body (strong conviction), 0.0 = pure wick (indecision).
    candle_body = abs(last_candle['Close'] - last_candle['Open'])
    candle_range = last_candle['High'] - last_candle['Low']
    body_ratio = candle_body / candle_range if candle_range > 0 else 0.0
    green_body = body_ratio > body_ratio_threshold

    logger.info(f'{YELLOW}volume: {volume:.2f}, mean_volume: {mean_volume:.2f}, current_pct: {current_pct:.2f}, price_threshold: {price_threshold:.2f}, body_ratio: {body_ratio:.2f}{RESET}')
    #3. Check conditions for buy/sell signals
    if volume > volume_threshold and current_pct > price_threshold and green_body:
        logger.info(f'{GREEN}BUY: candle_pct {current_pct:.2f}% > {price_threshold:.2f}% | volume {volume:.0f} > {volume_threshold:.0f} | body_ratio {body_ratio:.2f} > {body_ratio_threshold:.2f}{RESET}')
        SIGNAL = 'BUY'
    elif volume > volume_threshold and current_pct < -price_threshold and green_body:
        logger.info(f'{CYAN}SELL: candle_pct {current_pct:.2f}% < -{price_threshold:.2f}% | volume {volume:.0f} > {volume_threshold:.0f} | body_ratio {body_ratio:.2f} > {body_ratio_threshold:.2f}{RESET}')
        SIGNAL = 'SELL'
    else:
        SIGNAL = None

    green_volume = volume > volume_threshold
    green_price  = current_pct > price_threshold
    red_price    = current_pct < -price_threshold

    debug = {'volume': volume, 'mean_volume': mean_volume, 'current_pct': current_pct, 'price_threshold': price_threshold, 'body_ratio': body_ratio}
    flags = [green_volume, green_price, red_price, green_body]
    return SIGNAL, price, trail_stop_loss, debug, flags

def round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)

def get_tick_size(price: float, currency: str) -> float:
    if currency == 'USD':
        return 0.0001 if price < 1.0 else 0.01
    if price < 10:   return 0.01
    if price < 100:  return 0.05
    if price < 500:  return 0.10
    if price < 1000: return 0.50
    return 1.00

def timeframe_to_seconds(tf: str) -> int:
    if tf.endswith('m'):
        return int(tf[:-1]) * 60
    if tf.endswith('h'):
        return int(tf[:-1]) * 3600
    raise ValueError(f'Unsupported timeframe: {tf}')

# Usage: exchange_opening_time = get_exchange_opening_time(time.time())
def get_exchange_opening_time(now: float) -> float:
    """Return today's exchange open (9:30 ET) as a Unix timestamp comparable to time.time()."""
    now_dt = datetime.datetime.fromtimestamp(now, tz=EXCHANGE_TZ)
    opening_dt = datetime.datetime.combine(now_dt.date(), EXCHANGE_OPEN_TIME, tzinfo=EXCHANGE_TZ)
    return opening_dt.timestamp()

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
    init_signal_log(SIGNAL_LOG)
    gw.ib.execDetailsEvent += make_fill_handler(TRADE_LOG, '')

    def _on_fill(trade, fill):
        logger.info(
            f'FILL: {fill.execution.side} {fill.execution.shares} {trade.contract.symbol} '
            f'@ {fill.execution.avgPrice:.4f} | orderId={fill.execution.orderId}'
        )

    gw.ib.execDetailsEvent += _on_fill

    try:
        #1. Pobiera paramtry strategii z configs.py:
        config     = importlib.import_module(CONFIG_MODULE)
        params     = params_lookup.get_params(config.PARAMS, 'MomentumV8Strategy', SYMBOLS[0], TIMEFRAME)
        pd.set_option('display.max_rows', None)
        logger.debug(f'Parameters (shared for all symbols): {params}')

        vol_len = params.get('vol_len', 10)

        vol_multiplier = params.get('vol_multiplier', 1.8)
        price_move_pct = params.get('price_move_pct', 1.5)
        trail_stop_pct = params.get('trail_stop_pct', 1.0)
        body_ratio_threshold = params.get('body_ratio_threshold', 0.5)

        logger.debug(f"{YELLOW}vol_len = {vol_len}, vol_multiplier = {vol_multiplier}, price_move_pct = {price_move_pct}, trail_stop_pct={trail_stop_pct}, body_ratio_threshold={body_ratio_threshold}{RESET}")

        #2. Calculating timings for fetching data:
        tf_seconds = timeframe_to_seconds(TIMEFRAME)
        fetch_interval = tf_seconds                        # fetch once per bar
        duration = f'{vol_len * tf_seconds} S'             # enough bars to fill vol_len

        logger.debug(f'Monitoruję połączenie co {CHECK_INTERVAL} [s]. Wciśnij Ctrl+C aby zakończyć działanie programu.')
        last_fetch = 0
        last_processed_candle = {sym: None for sym in SYMBOLS}
        contracts = {sym: gw.make_stock_contract(sym, currency=SYMBOL_CURRENCY[sym]) for sym in SYMBOLS}
        while True:
            gw.ib.sleep(CHECK_INTERVAL)
            if not gw.ensure_connected():
                logger.error('Lost connection and could not reconnect. Exiting.')
                break
            #logger.debug('...')
            now = time.time()
            too_early = now < get_exchange_opening_time(now) + tf_seconds
            #logger.debug(f'tick: now={now:.0f}, last_fetch={last_fetch:.0f}, diff={now - last_fetch:.0f}s')
            if now - last_fetch >= fetch_interval:
                positions = gw.get_positions()
                for symbol in SYMBOLS:
                    #4. Ściągnij dane z IBKR
                    df = fetch_data_from_IBKR(gw, symbol, duration, TIMEFRAME, use_rth=True, currency=SYMBOL_CURRENCY[symbol])
                    if df is None:
                        logger.warning(f'{YELLOW}No data for {symbol}, skipping.{RESET}')
                        continue
                    df = df.tail(vol_len).copy()

                    #5. Sprawdź czy świeca już była przetworzona, jeśli tak to pomiń logikę wejścia
                    candle_time = df.iloc[-2].name
                    if candle_time == last_processed_candle[symbol]:
                        logger.debug(f'{YELLOW}{symbol}: candle {candle_time} already processed, skipping.{RESET}')
                        continue

                    #6. Entry logic
                    signal, _, trail_stop_loss, debug, flags = buy_or_sell(df, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold)
                    log_signal_csv(SIGNAL_LOG, symbol, signal, trail_stop_loss, debug, flags)
                    if not LIVE_TRADING:
                        logger.debug(f'{YELLOW}{symbol}: LIVE_TRADING is off, skipping entry.{RESET}')
                    elif too_early:
                        logger.debug(f'{YELLOW}{symbol}: within {tf_seconds // 60}min warm-up after open, skipping entry.{RESET}')
                    else:
                        execute_trade(gw, symbol, signal, contracts[symbol], QUANTITY, trail_stop_loss, FILL_TIMEOUT, positions)
                    last_processed_candle[symbol] = candle_time

                #7. Print positions
                current_positions = gw.get_positions()
                if current_positions:
                    for p in current_positions:
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
