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


# Usage: extreme, exit_time, exit_price = scan_trailing_stop(high_df, entry_time, entry_price, 'long', 1.2)
def scan_trailing_stop(high_df: pd.DataFrame, entry_time, entry_price: float, direction: str, trail_pct: float, extreme: float = None) -> tuple:
    """Walk bars forward from entry_time, updating the trailing peak/trough each bar and checking
    for a stop-hit using that bar's High/Low. `extreme` carries the running peak/trough across
    repeated calls — pass None (the default) on the first call to start fresh from entry_price,
    a backtest's one-shot call over the full history needs nothing else. A live caller checking
    periodically against only the newest bars should feed back the returned `extreme` on its next
    call, so the reference keeps tracking the true high/low since entry rather than resetting.
    Returns (extreme, exit_time, exit_price); exit_time/exit_price are (None, None) if the stop
    wasn't hit in the bars given."""
    if extreme is None:
        extreme = entry_price
    for ts, bar in high_df[high_df.index >= entry_time].iterrows():
        if direction == 'long':
            extreme = max(extreme, bar['High'])
            stop_price = extreme * (1 - trail_pct / 100)
            if bar['Low'] <= stop_price:
                return extreme, ts, stop_price
        else:
            extreme = min(extreme, bar['Low'])
            stop_price = extreme * (1 + trail_pct / 100)
            if bar['High'] >= stop_price:
                return extreme, ts, stop_price
    return extreme, None, None


# Usage: exit_time, exit_price = scan_take_profit(high_df, entry_time, entry_price, 'long', 2.4)
def scan_take_profit(high_df: pd.DataFrame, entry_time, entry_price: float, direction: str, tp_pct: float) -> tuple:
    """Walk bars forward from entry_time, checking each bar's High/Low against a fixed take-profit
    target set once at entry (entry_price ± tp_pct%). Unlike scan_trailing_stop's peak/trough, this
    target never moves, so — unlike that function — there's no running state to carry between
    repeated live calls; each call is independent. Returns (exit_time, exit_price); both are None
    if the target wasn't hit in the bars given."""
    if direction == 'long':
        target_price = entry_price * (1 + tp_pct / 100)
        for ts, bar in high_df[high_df.index >= entry_time].iterrows():
            if bar['High'] >= target_price:
                return ts, target_price
    else:
        target_price = entry_price * (1 - tp_pct / 100)
        for ts, bar in high_df[high_df.index >= entry_time].iterrows():
            if bar['Low'] <= target_price:
                return ts, target_price
    return None, None
