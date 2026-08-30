import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

import datetime
import importlib
from collections import deque
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway
import positions_observer as po
import params_lookup
from signal_checks import check_vol_price_body, scan_trailing_stop, scan_take_profit
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
LIVE_WINDOW_BARS = 12  # reqRealTimeBars() only ever hands back 5s bars; 12 of them = 1 minute
                        # between client-side trailing-stop / take-profit checks (per symbol)
TAKE_PROFIT_RR = 2.0   # experiment: take-profit target = trail_stop_loss × this risk:reward multiple

LOG_SUFFIX = f"{datetime.datetime.now(EXCHANGE_TZ).strftime('%Y%m%d_%H%M')}_{TIMEFRAME}"
TRADE_LOG = Path(f'logs/trades_{LOG_SUFFIX}.csv')
SIGNAL_LOG = Path(f'logs/signals_{LOG_SUFFIX}.csv')

# Usage: result = execute_trade(gw, 'RKLB', 'BUY', contract, 10, 0.5, 10, positions)
# Places the entry (+ a broker-side TRAIL as a safety net) and returns (entry_trade, trail_trade)
# once filled, or None if the signal was skipped (no signal / already in position / didn't fill).
def execute_trade(gw: IBKRGateway, symbol: str, signal: str, contract, quantity: int, trail_stop_loss: float, fill_timeout: float, positions: list):
    if not signal:
        return None
    already_in_position = any(getattr(p.contract, 'symbol', None) == symbol for p in positions)

    if already_in_position:
        logger.info(f'{YELLOW}Signal {signal} skipped — already in position.{RESET}')
        return None

    entry, tp, trail = gw.place_bracket_trailing(
        contract,
        action=signal,
        quantity=quantity,
        trail_percent=trail_stop_loss,
        fill_timeout=fill_timeout,
    )
    if trail is None:
        return None  # entry didn't fill
    po.add_position(symbol, entry.orderStatus.filled, signal, entry.orderStatus.avgFillPrice, contract)
    return entry, trail

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
    po.start_dashboard()
    po.sync_with_ibkr(gw.get_positions())
    def _on_ibkr_error(reqId, code, msg):
        # codes >= 2000 are connection/system info; 202 = order cancelled confirmation
        if code >= 2000 or code == 202:
            logger.debug(f'IBKR info {code} (reqId={reqId}): {msg}')
        else:
            logger.error(f'{RED}IBKR error {code} (reqId={reqId}): {msg}{RESET}')
    gw.on_error(_on_ibkr_error)
    #logging data
    init_trade_log(TRADE_LOG)
    init_signal_log(SIGNAL_LOG)
    gw.on_fill(make_fill_handler(TRADE_LOG, ''))

    # Client-side trailing-stop state, one entry per symbol currently in a trade — absent/None
    # while flat. Tracks the running peak/trough (see signal_checks.scan_trailing_stop) across
    # repeated live-bar checks. The broker-side TRAIL order placed by execute_trade() stays in
    # place as a safety net; this is a second, tighter check evaluated every LIVE_WINDOW_BARS
    # live bars (~1 minute) per symbol.
    open_trades: dict[str, dict] = {}
    live_bars: dict[str, deque] = {sym: deque(maxlen=LIVE_WINDOW_BARS) for sym in SYMBOLS}
    symbol_by_req_id: dict[int, str] = {}
    realtime_req_id_by_symbol: dict[str, int] = {}

    def _on_fill(trade, fill):
        logger.info(
            f'FILL: {fill.execution.side} {fill.execution.shares} {trade.contract.symbol} '
            f'@ {fill.execution.avgPrice:.4f} | orderId={fill.execution.orderId}'
        )
        # Any fill that isn't the tracked entry order is an exit — whether triggered by our own
        # client-side check (close_position), the broker-side TRAIL firing on its own, or
        # close_all_positions() flattening ahead of the close. Either way, stop tracking so the
        # next bar doesn't try to close an already-flat position.
        symbol = trade.contract.symbol
        open_trade = open_trades.get(symbol)
        if open_trade is not None and fill.execution.orderId != open_trade['entry_order_id']:
            logger.info(f'{YELLOW}Exit fill detected for {symbol} (orderId={fill.execution.orderId}) — clearing trade state.{RESET}')
            del open_trades[symbol]

    gw.on_fill(_on_fill)

    def _on_realtime_bar(reqId, bar):
        symbol = symbol_by_req_id.get(reqId)
        if symbol is None:
            return
        live_bars[symbol].append(bar)
        trade = open_trades.get(symbol)
        if trade is None:
            return
        trade['bars_since_check'] += 1
        if trade['bars_since_check'] < LIVE_WINDOW_BARS:
            return
        trade['bars_since_check'] = 0

        window_df = pd.DataFrame([{
            'Date': b.date, 'Open': b.open, 'High': b.high,
            'Low': b.low, 'Close': b.close, 'Volume': b.volume,
        } for b in live_bars[symbol]])
        window_df['Date'] = pd.to_datetime(window_df['Date'])
        window_df.set_index('Date', inplace=True)

        # Take-profit first — if it fires, skip the trailing-stop check this tick, there's
        # nothing left to trail.
        tp_time, tp_price = scan_take_profit(
            window_df, trade['entry_time'], trade['entry_price'], trade['direction'], trade['take_profit_pct'],
        )
        if tp_price is not None:
            logger.info(f'{GREEN}Client-side take-profit hit for {symbol} at {tp_price:.4f} — closing.{RESET}')
            try:
                gw.close_position(trade['contract'])
            except ValueError as e:
                logger.warning(f'{YELLOW}Could not close {symbol}: {e}{RESET}')
            return

        extreme, exit_time, exit_price = scan_trailing_stop(
            window_df, trade['entry_time'], trade['entry_price'], trade['direction'],
            trade['trail_stop_loss'], extreme=trade['extreme'],
        )
        trade['extreme'] = extreme
        if exit_price is not None:
            logger.info(f'{YELLOW}Client-side trailing stop hit for {symbol} at {exit_price:.4f} '
                        f'(extreme={extreme:.4f}) — closing.{RESET}')
            try:
                gw.close_position(trade['contract'])
            except ValueError as e:
                logger.warning(f'{YELLOW}Could not close {symbol}: {e}{RESET}')

    gw.on_realtime_bar(_on_realtime_bar)

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

        #2b. Subscribe to live 5s bars for every symbol — feeds the client-side trailing-stop
        # check in _on_realtime_bar(); entries below still run off the historical TIMEFRAME poll.
        for sym in SYMBOLS:
            req_id = gw.start_realtime_bars(contracts[sym], what_to_show='TRADES', use_rth=True)
            symbol_by_req_id[req_id] = sym
            realtime_req_id_by_symbol[sym] = req_id
        logger.info(f'Subscribed to live 5s bars for {len(SYMBOLS)} symbols.')

        while True:
            time.sleep(CHECK_INTERVAL)
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

                    #5. Sprawdź czy świeca już była przetworzona, jeśli tak to pomiń logikę wejścia
                    candle_time = df.iloc[-2].name
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
                        result = execute_trade(gw, symbol, signal, contracts[symbol], QUANTITY, trail_stop_loss, FILL_TIMEOUT, positions)
                        if result is not None:
                            entry, trail = result
                            open_trades[symbol] = {
                                'contract': contracts[symbol],
                                'direction': 'long' if signal == 'BUY' else 'short',
                                'entry_price': entry.orderStatus.avgFillPrice,
                                'entry_time': datetime.datetime.now(datetime.timezone.utc),
                                'entry_order_id': entry.order.orderId,
                                'trail_stop_loss': trail_stop_loss,
                                'extreme': entry.orderStatus.avgFillPrice,
                                'take_profit_pct': trail_stop_loss * TAKE_PROFIT_RR,
                                'bars_since_check': 0,
                            }
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
        for req_id in realtime_req_id_by_symbol.values():
            gw.stop_realtime_bars(req_id)
        gw.disconnect()

if __name__ == '__main__':
    main()
