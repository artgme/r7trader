import logging

logger = logging.getLogger(__name__)

import pandas as pd

YELLOW = '\033[33m'
GREEN  = '\033[32m'
CYAN   = '\033[36m'
RESET  = '\033[0m'


# Usage: signal, price, trail_stop_loss, debug, flags = check_vol_price_body(df, 1.8, 1.5, 1.0, 0.5)
def check_vol_price_body(df: pd.DataFrame, vol_multiplier: float, price_move_pct: float, trail_stop_pct: float, body_ratio_threshold: float) -> tuple:
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
