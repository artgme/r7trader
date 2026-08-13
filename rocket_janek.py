import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ib_insync').setLevel(logging.WARNING)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

import csv
import datetime
import importlib
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway
import positions_observer as po
import params_lookup
from signal_checks import check_vol_price_body
import time
from logging_functions import init_trade_log, make_fill_handler, init_signal_log, log_signal_csv, EXCHANGE_TZ
from common import RED, GREEN, YELLOW, BLUE, CYAN, WHITE, RESET, timeframe_to_seconds

CLIENT_ID=79

CONFIG_MODULE = 'tuner1_found_params3'  # swap to e.g. 'tuner1_found_params' to trade tuner-found params instead

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
FIXED_TRAIL_STOP_PCT = 0.5  # experiment: overrides the tuned/dynamic trail_stop_loss with a fixed value
EXCHANGE_OPEN_TIME = datetime.time(9, 30)
EXCHANGE_CLOSE_TIME = datetime.time(16, 0)
CLOSE_OVERNIGHT = True  # if True, flatten every open position shortly before the exchange closes — no overnight holds
CLOSE_BEFORE_SECONDS = 1200  # how far ahead of the close to flatten, when CLOSE_OVERNIGHT is on

LOG_SUFFIX = f"{datetime.datetime.now(EXCHANGE_TZ).strftime('%Y%m%d_%H%M')}_{TIMEFRAME}"
TRADE_LOG = Path(f'logs/trades_{LOG_SUFFIX}.csv')
SIGNAL_LOG = Path(f'logs/signals_{LOG_SUFFIX}.csv')

CANDLE_LOG_DIR = Path('ibkr_candles_log')
CANDLE_LOG_N = 15  # how many most-recent candles to log per fetch, oldest -> newest (fixed at the largest vol_len in use, so every symbol's full window is captured)

def execute_trade(gw: IBKRGateway, symbol: str, signal: str, contract, quantity: int, trail_stop_loss: float, fill_timeout: float, positions: list):
    if not signal:
        return
    already_in_position = any(getattr(p.contract, 'symbol', None) == symbol for p in positions)

    if already_in_position:
        logger.info(f'{YELLOW}Signal {signal} skipped — already in position.{RESET}')
        return

    entry, tp, trail = gw.place_bracket_trailing(
        contract,
        action=signal,
        quantity=quantity,
        trail_percent=trail_stop_loss,
        fill_timeout=fill_timeout,
    )
    if trail is not None:
        po.add_position(symbol, entry.orderStatus.filled, signal, entry.orderStatus.avgFillPrice, contract)

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

# Usage: exchange_opening_time = get_exchange_opening_time(time.time())
def get_exchange_opening_time(now: float) -> float:
    """Return today's exchange open (9:30 ET) as a Unix timestamp comparable to time.time()."""
    now_dt = datetime.datetime.fromtimestamp(now, tz=EXCHANGE_TZ)
    opening_dt = datetime.datetime.combine(now_dt.date(), EXCHANGE_OPEN_TIME, tzinfo=EXCHANGE_TZ)
    return opening_dt.timestamp()

# Usage: exchange_closing_time = get_exchange_closing_time(time.time())
def get_exchange_closing_time(now: float) -> float:
    """Return today's exchange close (16:00 ET) as a Unix timestamp comparable to time.time()."""
    now_dt = datetime.datetime.fromtimestamp(now, tz=EXCHANGE_TZ)
    closing_dt = datetime.datetime.combine(now_dt.date(), EXCHANGE_CLOSE_TIME, tzinfo=EXCHANGE_TZ)
    return closing_dt.timestamp()

# Usage: expected = expected_last_closed_candle(time.time(), 1800)
# Computed purely from wall-clock time and the market open, independent of whatever IBKR
# actually returns — used to detect when IBKR has handed back a stale candle.
def expected_last_closed_candle(now: float, tf_seconds: int) -> datetime.datetime | None:
    """Return the start-time of the most recently closed candle as of `now`, or None if
    less than one full candle has elapsed since open (nothing could have closed yet)."""
    open_ts = get_exchange_opening_time(now)
    elapsed = now - open_ts
    n = int(elapsed // tf_seconds)
    if n < 1:
        return None
    boundary_ts = open_ts + (n - 1) * tf_seconds
    return datetime.datetime.fromtimestamp(boundary_ts, tz=EXCHANGE_TZ)

# Flattens every open position via gw.close_position — used ahead of the exchange close so nothing is held overnight.
# close_position now waits for the fill, so a non-'Filled' status here means the position is still
# open and its trail/TP was already cancelled — i.e. naked. Surfaced loudly on purpose.
def close_all_positions(gw: IBKRGateway) -> None:
    for p in gw.get_positions():
        if p.position == 0:
            continue
        symbol = p.contract.symbol
        try:
            trade = gw.close_position(p.contract)
            if trade.orderStatus.status == 'Filled':
                logger.info(f'{YELLOW}Closed {symbol} ahead of exchange close.{RESET}')
            else:
                logger.error(f'{RED}{symbol} did NOT close (status={trade.orderStatus.status}) — position is open and unprotected.{RESET}')
        except ValueError as e:
            logger.warning(f'{YELLOW}Could not close {symbol}: {e}{RESET}')
    po.sync_with_ibkr(gw.get_positions())

def _candle_log_slot_label(i: int) -> str:
    if i == 0:
        return 'oldest'
    if i == CANDLE_LOG_N - 1:
        return 'newest'
    return f'newest-{CANDLE_LOG_N - 1 - i}'

# One file per ticker (ibkr_candles_log/{symbol}.csv) so each file's column count stays fixed —
# per-symbol vol_len varies, but this log always keeps the last CANDLE_LOG_N candles regardless.
# Same wide layout as bar_lag_forex.csv: one row per fetch, oldest -> newest, close+volume per slot.
def log_candles_wide(symbol: str, df: pd.DataFrame) -> None:
    CANDLE_LOG_DIR.mkdir(exist_ok=True)
    log_file = CANDLE_LOG_DIR / f'{symbol}.csv'
    last_n = df.tail(CANDLE_LOG_N)
    pad = CANDLE_LOG_N - len(last_n)  # missing oldest slots if fewer than CANDLE_LOG_N bars exist yet

    is_new = not log_file.exists()
    with log_file.open('a', newline='') as f:
        writer = csv.writer(f)
        if is_new:
            header = ['wall_clock']
            header += [f'bar_time({_candle_log_slot_label(i)})' for i in range(CANDLE_LOG_N)]
            for i in range(CANDLE_LOG_N):
                header.append(f'price_close({_candle_log_slot_label(i)})')
                header.append(f'volume({_candle_log_slot_label(i)})')
            writer.writerow(header)

        row = [datetime.datetime.now(EXCHANGE_TZ).isoformat()]
        row += [''] * pad + [ts.strftime('%H:%M:%S') for ts in last_n.index]
        row += [''] * (pad * 2)
        for _, r in last_n.iterrows():
            row.append(r['Close'])
            row.append(r['Volume'])
        writer.writerow(row)

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

    log_candles_wide(symbol, df)

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
    po.start_dashboard()
    po.sync_with_ibkr(gw.get_positions())
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
        pd.set_option('display.max_rows', None)

        #2. Calculating timings for fetching data:
        tf_seconds = timeframe_to_seconds(TIMEFRAME)
        fetch_interval = tf_seconds                        # fetch once per bar

        logger.debug(f'Monitoruję połączenie co {CHECK_INTERVAL} [s]. Wciśnij Ctrl+C aby zakończyć działanie programu.')
        last_fetch = 0
        last_processed_candle = {sym: None for sym in SYMBOLS}
        closed_overnight_on = None
        contracts = {sym: gw.make_stock_contract(sym, currency=SYMBOL_CURRENCY[sym]) for sym in SYMBOLS}
        while True:
            gw.ib.sleep(CHECK_INTERVAL)
            if not gw.ensure_connected():
                logger.error('Lost connection and could not reconnect. Exiting.')
                break
            #logger.debug('...')
            now = time.time()
            closing_time = get_exchange_closing_time(now)
            today = datetime.datetime.fromtimestamp(now, tz=EXCHANGE_TZ).date()
            if CLOSE_OVERNIGHT and closed_overnight_on != today and closing_time - CLOSE_BEFORE_SECONDS <= now < closing_time:
                logger.info(f'{YELLOW}CLOSE_OVERNIGHT: flattening all positions ({CLOSE_BEFORE_SECONDS // 60}min to exchange close).{RESET}')
                close_all_positions(gw)
                closed_overnight_on = today
            too_early = now < get_exchange_opening_time(now) + tf_seconds
            #logger.debug(f'tick: now={now:.0f}, last_fetch={last_fetch:.0f}, diff={now - last_fetch:.0f}s')
            if now - last_fetch >= fetch_interval:
                positions = gw.get_positions()
                for symbol in SYMBOLS:
                  try:
                    #3. Parametry per-symbol — każdy symbol ma własną strojoną konfigurację:
                    params = params_lookup.get_params(config.PARAMS, 'MomentumV8Strategy', symbol, TIMEFRAME)
                    vol_len = params.get('vol_len', 10)
                    vol_multiplier = params.get('vol_multiplier', 1.8)
                    price_move_pct = params.get('price_move_pct', 1.5)
                    trail_stop_pct = params.get('trail_stop_pct', 1.0)
                    body_ratio_threshold = params.get('body_ratio_threshold', 0.5)
                    duration = f'{vol_len * tf_seconds} S'          # enough bars to fill vol_len
                    logger.debug(f"{YELLOW}{symbol}: vol_len={vol_len}, vol_multiplier={vol_multiplier}, price_move_pct={price_move_pct}, trail_stop_pct={trail_stop_pct}, body_ratio_threshold={body_ratio_threshold}{RESET}")

                    #4. Ściągnij dane z IBKR
                    df = fetch_data_from_IBKR(gw, symbol, duration, TIMEFRAME, use_rth=True, currency=SYMBOL_CURRENCY[symbol])
                    if df is None:
                        logger.warning(f'{YELLOW}No data for {symbol}, skipping.{RESET}')
                        continue
                    df = df.tail(vol_len).copy()
                    po.update_current_price(symbol, df['Close'].iloc[-1])

                    #5. Zweryfikuj świeżość świecy, potem sprawdź czy już była przetworzona
                    candle_time = df.iloc[-2].name
                    expected = expected_last_closed_candle(now, tf_seconds)
                    if expected is not None and candle_time < expected:
                        logger.warning(f'{YELLOW}{symbol}: stale candle from IBKR ({candle_time} < expected {expected}), retrying next pass.{RESET}')
                        continue
                    if candle_time == last_processed_candle[symbol]:
                        logger.debug(f'{YELLOW}{symbol}: candle {candle_time} already processed, skipping.{RESET}')
                        continue

                    #6. Entry logic
                    signal, _, trail_stop_loss, debug, flags = check_vol_price_body(df, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold)
                    trail_stop_loss = FIXED_TRAIL_STOP_PCT  # experiment: fixed tight stop instead of the tuned/dynamic one
                    log_signal_csv(SIGNAL_LOG, symbol, signal, trail_stop_loss, debug, flags)
                    if not LIVE_TRADING:
                        logger.debug(f'{YELLOW}{symbol}: LIVE_TRADING is off, skipping entry.{RESET}')
                    elif too_early:
                        logger.debug(f'{YELLOW}{symbol}: within {tf_seconds // 60}min warm-up after open, skipping entry.{RESET}')
                    else:
                        execute_trade(gw, symbol, signal, contracts[symbol], QUANTITY, trail_stop_loss, FILL_TIMEOUT, positions)
                    last_processed_candle[symbol] = candle_time
                  except ConnectionError as e:
                    logger.error(f'{RED}{symbol}: connection error ({e}) — skipping this pass.{RESET}')
                    continue

                #7. Print positions
                current_positions = gw.get_positions()
                po.sync_with_ibkr(current_positions)
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
