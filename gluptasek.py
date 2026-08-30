import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

import datetime
from collections import deque
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway
import configs_rocketJanek as cfg
import params_lookup
import time
from logging_functions import init_trade_log, make_fill_handler
from signal_checks import check_vol_price_body, scan_trailing_stop, scan_take_profit

CLIENT_ID=78

CHECK_INTERVAL = 100  # sekundy pomiędzy sprawdzeniem połączenia
SYMBOL = 'RKLB' #ASM, BESI - EUR
TIMEFRAME = '10m'
QUANTITY = 100

LIVE_WINDOW_BARS = 12   # reqRealTimeBars() only ever hands back 5s bars; 12 of them = 1 minute
                         # between client-side trailing-stop / take-profit checks
TAKE_PROFIT_RR = 2.0    # experiment: take-profit target = trail_stop_loss × this risk:reward multiple


TRADE_LOG = Path('logs/trades_rklb_gluptasek_26Jun2.csv')

RED    = '\033[31m'
GREEN  = '\033[32m'
YELLOW = '\033[33m'
BLUE   = '\033[34m'
CYAN   = '\033[36m'
WHITE  = '\033[37m'
RESET  = '\033[0m'

# Usage: entry = execute_trade(gw, 'RKLB', 'BUY', contract, 100, 12.34, 0.8, 0.01)
# Places the entry (+ a broker-side TRAIL as a safety net) and returns (entry_trade, trail_trade)
# once filled, or None if the signal was skipped (no signal / already in position / didn't fill).
def execute_trade(gw: IBKRGateway, symbol: str, signal: str, contract, quantity: int, price: float, trail_stop_loss: float, tick_size: float):
    if not signal:
        return None
    positions = gw.get_positions()
    already_in_position = any(getattr(p.contract, 'symbol', None) == symbol for p in positions)
    if already_in_position:
        logger.info(f'{YELLOW}Signal {signal} skipped — already in position.{RESET}')
        return None

    limit_mult = 0.995 if signal == 'SELL' else 1.005
    entry, tp, trail = gw.place_bracket_trailing(
        contract,
        action=signal,
        quantity=quantity,
        limit_price=round_to_tick(price * limit_mult, tick_size),
        trail_percent=trail_stop_loss,
    )
    if trail is None:
        return None  # entry didn't fill
    return entry, trail

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
    def _on_ibkr_error(reqId, code, msg):
        # codes >= 2000 are connection/system info; 202 = order cancelled confirmation
        if code >= 2000 or code == 202:
            logger.debug(f'IBKR info {code} (reqId={reqId}): {msg}')
        else:
            logger.error(f'{RED}IBKR error {code} (reqId={reqId}): {msg}{RESET}')
    gw.on_error(_on_ibkr_error)
    #logging data
    init_trade_log(TRADE_LOG)
    gw.on_fill(make_fill_handler(TRADE_LOG, SYMBOL))

    def _on_fill(trade, fill):
        logger.info(
            f'FILL: {fill.execution.side} {fill.execution.shares} {trade.contract.symbol} '
            f'@ {fill.execution.avgPrice:.4f} | orderId={fill.execution.orderId}'
        )
        # Any fill that isn't the tracked entry order is an exit — whether triggered by our own
        # client-side check (close_position) or the broker-side TRAIL firing on its own. Either
        # way, stop tracking so the next bar doesn't try to close an already-flat position.
        open_trade = trade_state['trade']
        if open_trade is not None and fill.execution.orderId != open_trade['entry_order_id']:
            logger.info(f'{YELLOW}Exit fill detected for {SYMBOL} (orderId={fill.execution.orderId}) — clearing trade state.{RESET}')
            trade_state['trade'] = None

    gw.on_fill(_on_fill)

    # Client-side trailing-stop state — 'trade' is None while flat, else a dict tracking the
    # running peak/trough (see signal_checks.scan_trailing_stop) across repeated live-bar checks.
    # The broker-side TRAIL order placed by execute_trade() stays in place as a safety net; this
    # is a second, tighter check evaluated every LIVE_WINDOW_BARS live bars (~1 minute).
    trade_state = {'trade': None, 'bars_since_check': 0}
    live_bars = deque(maxlen=LIVE_WINDOW_BARS)
    realtime_req_id = None

    def _on_realtime_bar(reqId, bar):
        live_bars.append(bar)
        trade = trade_state['trade']
        if trade is None:
            return
        trade_state['bars_since_check'] += 1
        if trade_state['bars_since_check'] < LIVE_WINDOW_BARS:
            return
        trade_state['bars_since_check'] = 0

        window_df = pd.DataFrame([{
            'Date': b.date, 'Open': b.open, 'High': b.high,
            'Low': b.low, 'Close': b.close, 'Volume': b.volume,
        } for b in live_bars])
        window_df['Date'] = pd.to_datetime(window_df['Date'])
        window_df.set_index('Date', inplace=True)

        # Take-profit first — if it fires, skip the trailing-stop check this tick, there's
        # nothing left to trail.
        tp_time, tp_price = scan_take_profit(
            window_df, trade['entry_time'], trade['entry_price'], trade['direction'], trade['take_profit_pct'],
        )
        if tp_price is not None:
            logger.info(f'{GREEN}Client-side take-profit hit for {SYMBOL} at {tp_price:.4f} — closing.{RESET}')
            try:
                gw.close_position(trade['contract'])
            except ValueError as e:
                logger.warning(f'{YELLOW}Could not close {SYMBOL}: {e}{RESET}')
            return

        extreme, exit_time, exit_price = scan_trailing_stop(
            window_df, trade['entry_time'], trade['entry_price'], trade['direction'],
            trade['trail_stop_loss'], extreme=trade['extreme'],
        )
        trade['extreme'] = extreme
        if exit_price is not None:
            logger.info(f'{YELLOW}Client-side trailing stop hit for {SYMBOL} at {exit_price:.4f} '
                        f'(extreme={extreme:.4f}) — closing.{RESET}')
            try:
                gw.close_position(trade['contract'])
            except ValueError as e:
                logger.warning(f'{YELLOW}Could not close {SYMBOL}: {e}{RESET}')

    gw.on_realtime_bar(_on_realtime_bar)

    try:
        #1. Pobiera parametry strategii z configs.py:
        params     = params_lookup.get_params(cfg.PARAMS, 'MomentumV8Strategy', SYMBOL, TIMEFRAME)
        pd.set_option('display.max_rows', None)
        logger.debug(f'Parameters for {SYMBOL}: {params}')

        currency_par = params.get('currency', 'USD')
        vol_len = params.get('vol_len', 10)

        vol_multiplier = params.get('vol_multiplier', 1.5)
        price_move_pct = params.get('price_move_pct', 1.0)
        trail_stop_pct = params.get('trail_stop_pct', 0.2)
        body_ratio_threshold = params.get('body_ratio_threshold', 0.5)
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

        #3b. Subscribe to live 5s bars — feeds the client-side trailing-stop check in
        # _on_realtime_bar(); entries below still run off the historical TIMEFRAME poll.
        realtime_req_id = gw.start_realtime_bars(contract, what_to_show='TRADES', use_rth=True)
        logger.info(f'Subscribed to live 5s bars for {SYMBOL} (reqId={realtime_req_id}).')

        while True:
            # Interleave sleep and plt.pause (GUI) so the plot window stays responsive.
            for _ in range(CHECK_INTERVAL):
                time.sleep(0.9)
                plt.pause(0.1)
            if not gw.ensure_connected():
                logger.error('Lost connection and could not reconnect. Exiting.')
                break
            #logger.debug('...')
            now = time.time()
            #logger.debug(f'tick: now={now:.0f}, last_fetch={last_fetch:.0f}, diff={now - last_fetch:.0f}s')
            if now - last_fetch >= fetch_interval: #5 minutes * 60 = 300s; 10min *60 =600s
                #4. Ściągnij dane z IBKR
                df = fetch_data_from_IBKR(gw, SYMBOL, duration, TIMEFRAME, use_rth=True, currency=currency_par) #12 x 10 min = 120 min <= 7200 s
                if df is None: #In case there are no new bars, skip the rest of the loop
                    last_fetch = now
                    continue
                df = df.tail(vol_len).copy()

                #5. Sprawdź czy świeca już była przetworzona, jeśli tak to pomiń logikę wejścia
                last_candle = df.iloc[-2]
                candle_time = last_candle.name  # DatetimeIndex — unique per candle
                if candle_time == last_processed_candle:
                    logger.debug(f'{YELLOW}Candle {candle_time} already processed, skipping.{RESET}')
                else:
                    #Entry logic — still historical-data-driven, per signal_checks.check_vol_price_body()
                    signal, price, trail_stop_loss, _debug, _flags = check_vol_price_body(
                        df, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold)
                    result = execute_trade(gw, SYMBOL, signal, contract, QUANTITY, price, trail_stop_loss, tick_size)
                    if result is not None:
                        entry, trail = result
                        trade_state['trade'] = {
                            'contract': contract,
                            'direction': 'long' if signal == 'BUY' else 'short',
                            'entry_price': entry.orderStatus.avgFillPrice,
                            'entry_time': datetime.datetime.now(datetime.timezone.utc),
                            'entry_order_id': entry.order.orderId,
                            'trail_stop_loss': trail_stop_loss,
                            'extreme': entry.orderStatus.avgFillPrice,
                            'take_profit_pct': trail_stop_loss * TAKE_PROFIT_RR,
                        }
                        trade_state['bars_since_check'] = 0
                    last_processed_candle = candle_time
                
                #6. printing positions
                positions = gw.get_positions()
                if positions:
                    for p in positions:
                        logger.debug(f'{BLUE}Position: {p}{RESET}')
                else:
                    logger.debug(f'{YELLOW}No open positions.{RESET}')

                last_fetch = now #update timer
                #logger.debug(f'Next fetch in 300s at {time.strftime("%H:%M:%S", time.localtime(last_fetch + 300))}')

    except KeyboardInterrupt:
        logger.info('Stopped by user.')
    finally:
        if realtime_req_id is not None:
            gw.stop_realtime_bars(realtime_req_id)
        gw.disconnect()


if __name__ == '__main__':
    main()
