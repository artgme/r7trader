import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ib_insync').setLevel(logging.WARNING)
logging.getLogger('ibkr').setLevel(logging.INFO)

import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from ibkr import IBKRGateway
from rocket_janek import buy_or_sell, timeframe_to_seconds, RED, GREEN, WHITE, RESET
from printing_results import fetch_and_plot
import configs_rocketJanek as cfg

CLIENT_ID = 81
TICKER = 'AAPL'
CURRENCY = 'USD'
TIMEFRAME = '30m'
START_DT = datetime.datetime(2026, 7, 13, 9, 30, tzinfo=ZoneInfo('America/New_York'))
END_DAY = datetime.date(2026, 7, 14)
QUANTITY = 10
FETCH_AND_PLOT = 1

EXCHANGE_TZ = ZoneInfo('America/New_York')


# Usage: df = fetch_day(gw, date(2026, 6, 29), 'RKLB', 'USD', '10m')
def fetch_day(gw: IBKRGateway, date: datetime.date, ticker: str, currency: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch the full RTH session for `date`."""
    end_dt = datetime.datetime.combine(date, datetime.time(23, 59, 59), tzinfo=EXCHANGE_TZ)
    contract = gw.make_stock_contract(ticker, currency=currency)
    bars = gw.fetch_historical(contract, duration='1 D', bar_size=timeframe, use_rth=True, end_dt=end_dt)
    if not bars:
        return None
    df = pd.DataFrame([{
        'Date': b.date, 'Open': b.open, 'High': b.high,
        'Low': b.low, 'Close': b.close, 'Volume': b.volume,
    } for b in bars])
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    return df


# Usage: df = fetch_range(gw, 'TEAM', 'USD', date(2026,7,8), date(2026,7,10), '30m')
def fetch_range(gw: IBKRGateway, ticker: str, currency: str, start_day: datetime.date, end_day: datetime.date, timeframe: str) -> pd.DataFrame:
    """Fetch and concatenate RTH sessions for every weekday from start_day through end_day."""
    frames = []
    day = start_day
    while day <= end_day:
        if day.weekday() < 5:  # skip weekends
            df = fetch_day(gw, day, ticker, currency, timeframe)
            if df is not None and not df.empty:
                frames.append(df)
        day += datetime.timedelta(days=1)
    return pd.concat(frames) if frames else pd.DataFrame()


def _scan_trailing_stop(high_df: pd.DataFrame, entry_time, entry_price: float, direction: str, trail_pct: float):
    """Walk 1m bars forward from entry_time, updating the trailing peak/trough each bar and
    checking for a stop-hit using that bar's High/Low. Returns (exit_time, exit_price), or
    (None, None) if the stop was never hit before the data ran out."""
    extreme = entry_price
    for ts, bar in high_df[high_df.index >= entry_time].iterrows():
        if direction == 'long':
            extreme = max(extreme, bar['High'])
            stop_price = extreme * (1 - trail_pct / 100)
            if bar['Low'] <= stop_price:
                return ts, stop_price
        else:
            extreme = min(extreme, bar['Low'])
            stop_price = extreme * (1 + trail_pct / 100)
            if bar['High'] >= stop_price:
                return ts, stop_price
    return None, None


# Usage: marker_trades = to_marker_trades(trades)
def to_marker_trades(trades: list[dict]) -> list[dict]:
    """Convert run_backtest()'s trade dicts into the enter/exit marker format fetch_and_plot() expects."""
    marker_trades = []
    for t in trades:
        entry_action = 'enter_long' if t['direction'] == 'long' else 'enter_short'
        marker_trades.append({'date': t['entry_time'], 'action': entry_action, 'price': t['entry_price']})
        if t['exit_time'] is not None:
            exit_action = 'exit_long_trail' if t['direction'] == 'long' else 'exit_short_trail'
            marker_trades.append({'date': t['exit_time'], 'action': exit_action, 'price': t['exit_price']})
    return marker_trades


# Usage: trades, checks = run_backtest(symbol, low_df, high_df, start_dt, timeframe, vol_len,
#                                       vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, quantity)
def run_backtest(symbol: str, low_df: pd.DataFrame, high_df: pd.DataFrame, start_dt, timeframe: str,
                  vol_len: int, vol_multiplier: float, price_move_pct: float, trail_stop_pct: float,
                  body_ratio_threshold: float, quantity: float) -> tuple[list[dict], list[dict]]:
    """Walk low_df candle-by-candle, calling buy_or_sell() on each closed candle while flat —
    same window shape as the live loop (iloc[-2] = signal candle, iloc[-1] = next candle,
    standing in for the still-forming candle a live fetch would see). On a signal, fills at
    the next available 1m price and scans high_df for a trailing-stop exit before resuming.
    Returns (trades, checks) — checks records every candle evaluated while flat, signal or not,
    so a day with zero trades still shows why nothing fired."""
    bar_duration = pd.Timedelta(seconds=timeframe_to_seconds(timeframe))
    trades = []
    checks = []
    i = vol_len - 2  # earliest index with a full vol_len window ending at i+1
    n = len(low_df)
    while i < n - 1:
        # Don't evaluate signals before the requested scan start, even though we may have
        # fetched earlier days purely to give the first real window enough history.
        if low_df.index[i] < start_dt:
            i += 1
            continue

        # Same window shape buy_or_sell() expects live: vol_len bars, candle i is iloc[-2]
        # (the signal candle), candle i+1 stands in for the still-forming iloc[-1] candle.
        window = low_df.iloc[i - vol_len + 2: i + 2].copy()
        signal, _, trail_stop_loss, debug, flags = buy_or_sell(window, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold)
        green_volume, green_price, red_price, green_body = flags
        checks.append({
            'symbol': symbol,
            'signal_time': low_df.index[i],
            'signal': signal or 'none',
            'volume': debug['volume'],
            'mean_volume': debug['mean_volume'],
            'current_pct': debug['current_pct'],
            'price_threshold': debug['price_threshold'],
            'trail_stop_pct': trail_stop_loss,
            'body_ratio': debug['body_ratio'],
            'green_volume': green_volume,
            'green_price': green_price,
            'red_price': red_price,
            'green_body': green_body,
        })
        if not signal:
            i += 1
            continue

        # Signal fires once candle i actually closes; fill at the first 1m price available
        # after that moment, mirroring the live market order placed right after the fetch.
        close_time = low_df.index[i] + bar_duration
        entry_bars = high_df[high_df.index >= close_time]
        if entry_bars.empty:
            i += 1
            continue
        entry_time = entry_bars.index[0]
        entry_price = entry_bars.iloc[0]['Open']
        direction = 'long' if signal == 'BUY' else 'short'

        # Walk 1m bars forward until the trailing stop is hit (or data runs out).
        exit_time, exit_price = _scan_trailing_stop(high_df, entry_time, entry_price, direction, trail_stop_loss)
        if exit_price is not None:
            pnl = (exit_price - entry_price) * quantity if direction == 'long' else (entry_price - exit_price) * quantity
        else:
            pnl = None

        trades.append({
            'symbol': symbol,
            'direction': direction,
            'signal_time': low_df.index[i],
            'entry_time': entry_time,
            'entry_price': entry_price,
            'trail_stop_pct': trail_stop_loss,
            'exit_time': exit_time,
            'exit_price': exit_price,
            'quantity': quantity,
            'pnl': pnl,
        })

        if exit_time is None:
            break  # position never closed before the data ran out
        # Resume scanning for the next signal once flat again.
        i = low_df.index.searchsorted(exit_time, side='left')

    return trades, checks


def main():
    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        logger.error('Could not connect to IBKR.')
        return

    fetch_start_day = START_DT.date() - datetime.timedelta(days=5)  # extra lookback so vol_len has history
    try:
        low_df = fetch_range(gw, TICKER, CURRENCY, fetch_start_day, END_DAY, TIMEFRAME)
        high_df = fetch_range(gw, TICKER, CURRENCY, fetch_start_day, END_DAY, '1m')
    finally:
        gw.disconnect()

    if low_df.empty or high_df.empty:
        logger.error('No data fetched — exiting.')
        return

    # Same params lookup as rocket_janek.py's main(): shared across all symbols, sourced from RKLB's config.
    params = cfg.get_params('MomentumV8Strategy', 'RKLBUUU', TIMEFRAME)
    vol_len = params.get('vol_len', 10)
    vol_multiplier = params.get('vol_multiplier', 1.2)
    price_move_pct = params.get('price_move_pct', 1.1)
    trail_stop_pct = params.get('trail_stop_pct', 1.6)
    body_ratio_threshold = params.get('body_ratio_threshold', 0.5)

    trades, checks = run_backtest(TICKER, low_df, high_df, START_DT, TIMEFRAME, vol_len,
                                   vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, QUANTITY)

    print(f'\n{TICKER} backtest: {START_DT} to {END_DAY}, {TIMEFRAME} signal / 1m exit, {len(trades)} trade(s)\n')
    cumulative = 0.0
    for i, t in enumerate(trades, 1):
        exit_str = f"{t['exit_price']:.2f} @ {t['exit_time']}" if t['exit_price'] is not None else 'OPEN (never exited)'
        if t['pnl'] is not None:
            cumulative += t['pnl']
            pnl_str = f"{t['pnl']:+.2f}  cum {cumulative:+.2f}"
        else:
            pnl_str = 'n/a'
        print(f"  #{i:2d}  {t['direction']:5s}  signal {t['signal_time']}  "
              f"entry {t['entry_price']:.2f} @ {t['entry_time']}  exit {exit_str}  "
              f"trail {t['trail_stop_pct']:.2f}%  pnl {pnl_str}")

    # Every candle evaluated while flat, whether or not it fired — shows why a signal didn't
    # trigger just as clearly as why one did.
    print(f"\n  {'#':>3}  {'signal_time':25}  {'signal':6}  {'volume':>10}  {'mean_volume':>12}  {'current_pct':>12}  {'price_threshold':>16}  {'trail_stop_pct':>15}  {'body_ratio':>11}")
    for i, c in enumerate(checks, 1):
        # Pad the plain text to fixed width first, then wrap in color — ANSI codes would
        # otherwise count toward the f-string width and break column alignment.
        volume_color = GREEN if c['green_volume'] else WHITE
        volume_str = f"{volume_color}{c['volume']:>10.0f}{RESET}"
        pct_color = GREEN if c['green_price'] else RED if c['red_price'] else WHITE
        pct_str = f"{pct_color}{c['current_pct']:>+11.2f}%{RESET}"
        body_color = GREEN if c['green_body'] else WHITE
        body_str = f"{body_color}{c['body_ratio']:>11.2f}{RESET}"
        print(f"  {i:>3}  {str(c['signal_time']):25}  {c['signal']:6}  "
              f"{volume_str}  {c['mean_volume']:>12.0f}  {pct_str}  {c['price_threshold']:>15.2f}%  {c['trail_stop_pct']:>14.2f}%  {body_str}")

    if trades and FETCH_AND_PLOT:
        # fetch_and_plot() plots a single day — fine for a same-day backtest run like this one,
        # but trades spanning multiple days would only show whichever day START_DT falls on.
        fetch_and_plot(TICKER, CURRENCY, to_marker_trades(trades), START_DT.date(), TIMEFRAME)


if __name__ == '__main__':
    main()
