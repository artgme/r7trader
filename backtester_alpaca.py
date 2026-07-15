import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)

import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from rocket_janek import buy_or_sell, timeframe_to_seconds, RED, GREEN, WHITE, RESET
import configs_rocketJanek as cfg

load_dotenv()
ALPACA_API_KEY = os.environ.get('ALPACA_API_KEY')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_API_SECRET')

TICKER = 'RKLB'
CURRENCY = 'USD'
TIMEFRAME = '30m'
START_DT = datetime.datetime(2026, 7, 1, 9, 30, tzinfo=ZoneInfo('America/New_York'))
END_DAY = datetime.date(2026, 7, 14)
QUANTITY = 10
FETCH_AND_PLOT = 0

EXCHANGE_TZ = ZoneInfo('America/New_York')
RTH_OPEN = datetime.time(9, 30)
RTH_CLOSE = datetime.time(16, 0)

# action → (marker, color) — copied from printing_results.py, data-source-agnostic
MARKER_STYLE = {
    'enter_long':       ('^', 'green'),
    'enter_short':      ('v', 'red'),
    'exit_long_trail':  ('x', 'lime'),
    'exit_short_trail': ('x', 'orange'),
}


# Usage: _parse_timeframe('30m') -> Timedelta('0 days 00:30:00')
def _parse_timeframe(tf: str) -> pd.Timedelta:
    if tf.endswith('m'):
        return pd.Timedelta(minutes=int(tf[:-1]))
    if tf.endswith('h'):
        return pd.Timedelta(hours=int(tf[:-1]))
    raise ValueError(f'Unsupported timeframe: {tf}')


# Usage: _parse_alpaca_timeframe('30m') -> TimeFrame(30, TimeFrameUnit.Minute)
def _parse_alpaca_timeframe(tf: str) -> TimeFrame:
    if tf.endswith('m'):
        return TimeFrame(int(tf[:-1]), TimeFrameUnit.Minute)
    if tf.endswith('h'):
        return TimeFrame(int(tf[:-1]), TimeFrameUnit.Hour)
    raise ValueError(f'Unsupported timeframe: {tf}')


# Usage: df = fetch_range(client, 'AAPL', date(2026,7,8), date(2026,7,14), '30m')
def fetch_range(client: StockHistoricalDataClient, ticker: str, start_day: datetime.date,
                 end_day: datetime.date, timeframe: str) -> pd.DataFrame:
    """Fetch RTH bars for [start_day, end_day] in a single request (Alpaca has no per-day
    duration limit like IBKR), normalized to the same shape rocket_janek.buy_or_sell() expects:
    tz-aware (America/New_York) DatetimeIndex named 'Date', columns Open/High/Low/Close/Volume."""
    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=_parse_alpaca_timeframe(timeframe),
        start=datetime.datetime.combine(start_day, datetime.time.min, tzinfo=EXCHANGE_TZ),
        end=datetime.datetime.combine(end_day, datetime.time.max, tzinfo=EXCHANGE_TZ),
        feed='iex',  # free-tier data feed
    )
    bars = client.get_stock_bars(request)
    df = bars.df
    if df.empty:
        return pd.DataFrame()
    df = df.xs(ticker, level='symbol')
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'})
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    df.index = df.index.tz_convert(EXCHANGE_TZ)
    df.index.name = 'Date'
    df = df[(df.index.time >= RTH_OPEN) & (df.index.time < RTH_CLOSE)]  # Alpaca includes extended hours by default
    return df


# Usage: exit_time, exit_price = _scan_trailing_stop(high_df, entry_time, entry_price, 'long', 1.2)
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


# Usage: idx = _bar_idx(df, trade['date'])
def _bar_idx(df: pd.DataFrame, ts) -> int:
    """Return the bar index containing ts, converting ts to df.index timezone first."""
    ts_cmp = ts.tz_convert(df.index.tz) if df.index.tz is not None else ts.replace(tzinfo=None)
    return df.index.searchsorted(ts_cmp, side='right') - 1


# Usage: addplots = make_trade_addplots(df, trades, ax=ax)
def make_trade_addplots(df: pd.DataFrame, trades: list[dict], ax=None) -> list:
    """Build mplfinance addplot marker objects from trade list.
    Marker shapes: ^ enter_long (green), v enter_short (red), x exits (lime/orange).
    Pass ax= when using mplfinance external axes mode."""
    addplots = []
    for action, (marker, color) in MARKER_STYLE.items():
        prices = pd.Series(float('nan'), index=df.index)
        for trade in trades:
            if trade['action'] != action:
                continue
            idx = _bar_idx(df, trade['date'])
            if idx < 0 or idx >= len(df):
                continue
            prices.iloc[idx] = trade['price']
        if not prices.isna().all():
            ap_kwargs = dict(type='scatter', markersize=100, marker=marker, color=color)
            if ax is not None:
                ap_kwargs['ax'] = ax
            addplots.append(mpf.make_addplot(prices, **ap_kwargs))
    return addplots


# Usage: annotate_trades(axes[0], df, trades)
def annotate_trades(ax, df: pd.DataFrame, trades: list[dict]) -> None:
    """Add Entry/Exit text labels next to each trade marker on the price axis."""
    for trade in trades:
        idx = _bar_idx(df, trade['date'])
        if idx < 0 or idx >= len(df):
            continue
        is_entry = trade['action'].startswith('enter')
        label  = 'Entry' if is_entry else 'Exit'
        color  = 'green' if is_entry else 'lime'
        offset = (0, 8)  if is_entry else (0, -8)
        va     = 'bottom' if is_entry else 'top'
        ax.annotate(
            label,
            xy=(idx, trade['price']),
            xytext=offset,
            textcoords='offset points',
            fontsize=8, color=color, ha='center', va=va,
        )


def fetch_and_plot(client: StockHistoricalDataClient, ticker: str, trades: list[dict], start_day: datetime.date, end_day: datetime.date, timeframe: str = TIMEFRAME) -> None:
    entry_trades = [t for t in trades if t['action'].startswith('enter')]
    exit_trades  = [t for t in trades if t['action'].startswith('exit')]

    df_big = fetch_range(client, ticker, start_day, end_day, timeframe)
    df_1m  = fetch_range(client, ticker, start_day, end_day, '1m')

    if df_big is None or df_big.empty:
        logger.error('No %s candle data — exiting.', timeframe)
        return
    if df_1m is None or df_1m.empty:
        logger.error('No 1m candle data — exiting.')
        return

    big_tf_duration = _parse_timeframe(timeframe)
    one_min = pd.Timedelta(minutes=1)

    fig = plt.figure(figsize=(14, 14))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1, 3, 1], hspace=0.4)
    ax_big     = fig.add_subplot(gs[0])
    ax_big_vol = fig.add_subplot(gs[1], sharex=ax_big)
    ax_1m      = fig.add_subplot(gs[2])
    ax_1m_vol  = fig.add_subplot(gs[3], sharex=ax_1m)
    fig.suptitle(f'{ticker} — {start_day} to {end_day} (Alpaca)')

    # Top subplot: big-timeframe candles with entry markers
    entry_addplots = make_trade_addplots(df_big, entry_trades, ax=ax_big)
    mpf_kwargs = dict(type='candle', ax=ax_big, volume=ax_big_vol, style='charles')
    if entry_addplots:
        mpf_kwargs['addplot'] = entry_addplots
    mpf.plot(df_big, **mpf_kwargs)
    ax_big.set_title(f'{timeframe} — entries')
    annotate_trades(ax_big, df_big, entry_trades)

    # Bottom subplot: 1m candles with exit markers
    exit_addplots = make_trade_addplots(df_1m, exit_trades, ax=ax_1m)
    mpf_kwargs = dict(type='candle', ax=ax_1m, volume=ax_1m_vol, style='charles')
    if exit_addplots:
        mpf_kwargs['addplot'] = exit_addplots
    mpf.plot(df_1m, **mpf_kwargs)
    ax_1m.set_title('1m — exits')
    annotate_trades(ax_1m, df_1m, exit_trades)

    for ax in (ax_big, ax_big_vol, ax_1m, ax_1m_vol):
        ax.grid(axis='x', color='gray', linestyle='--', alpha=0.4, linewidth=0.5)

    # Synchronize x-axes by time — mplfinance uses integer bar positions internally,
    # so we convert bar index range → [start, end) time window → bar index range in the
    # other chart. The window's end must be the *end* of the last visible bar (start +
    # its own duration), not its start, or a coarse→fine sync collapses to ~0 width.
    _syncing = [False]

    def _sync_xlim(src_ax, src_df, src_bar_duration, dst_ax, dst_df):
        xmin, xmax = src_ax.get_xlim()
        i_min = max(0, min(int(xmin), len(src_df) - 1))
        i_max = max(0, min(int(xmax), len(src_df) - 1))
        t_start = src_df.index[i_min].tz_convert(dst_df.index.tz)
        t_end   = (src_df.index[i_max] + src_bar_duration).tz_convert(dst_df.index.tz)
        j_min = max(0, dst_df.index.searchsorted(t_start, side='right') - 1)
        j_max = max(0, dst_df.index.searchsorted(t_end, side='left') - 1)
        dst_ax.set_xlim(j_min - 0.5, j_max + 0.5)

    def _sync_to_1m(_):
        if _syncing[0]: return
        _syncing[0] = True
        try:
            _sync_xlim(ax_big, df_big, big_tf_duration, ax_1m, df_1m)
        finally:
            _syncing[0] = False

    def _sync_to_big(_):
        if _syncing[0]: return
        _syncing[0] = True
        try:
            _sync_xlim(ax_1m, df_1m, one_min, ax_big, df_big)
        finally:
            _syncing[0] = False

    ax_big.callbacks.connect('xlim_changed', _sync_to_1m)
    ax_1m.callbacks.connect('xlim_changed', _sync_to_big)

    _sync_to_1m(None)  # initial sync so both charts start aligned

    plt.tight_layout()
    plt.show()


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.error('Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env before running this.')
        return
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    fetch_start_day = START_DT.date() - datetime.timedelta(days=5)  # extra lookback so vol_len has history
    low_df = fetch_range(client, TICKER, fetch_start_day, END_DAY, TIMEFRAME)
    high_df = fetch_range(client, TICKER, fetch_start_day, END_DAY, '1m')

    if low_df.empty or high_df.empty:
        logger.error('No data fetched — exiting.')
        return

    # Same params lookup as rocket_janek.py's main(): shared across all symbols, sourced from RKLB's config.
    # params = cfg.get_params('MomentumV8Strategy', 'RKLB', TIMEFRAME)
    # vol_len = params.get('vol_len', 10)
    # vol_multiplier = params.get('vol_multiplier', 1.8)
    # price_move_pct = params.get('price_move_pct', 1.5)
    # trail_stop_pct = params.get('trail_stop_pct', 1.0)
    # body_ratio_threshold = params.get('body_ratio_threshold', 0.5)
    vol_len = 10
    vol_multiplier = 1.8
    price_move_pct = 1.5
    trail_stop_pct = 1.8
    body_ratio_threshold = 0.5

    trades, checks = run_backtest(TICKER, low_df, high_df, START_DT, TIMEFRAME, vol_len,
                                   vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, QUANTITY)

    print(f'\n{TICKER} backtest (Alpaca data): {START_DT} to {END_DAY}, {TIMEFRAME} signal / 1m exit, {len(trades)} trade(s)\n')
    cumulative = 0.0
    for i, t in enumerate(trades, 1):
        exit_str = f"{t['exit_price']:.2f} @ {t['exit_time']}" if t['exit_price'] is not None else 'OPEN (never exited)'
        if t['pnl'] is not None:
            cumulative += t['pnl']
            cum_color = GREEN if cumulative >= 0 else RED
            pnl_str = f"{t['pnl']:+.2f}  cum {cum_color}{cumulative:+.2f}{RESET}"
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
        fetch_and_plot(client, TICKER, to_marker_trades(trades), START_DT.date(), END_DAY, TIMEFRAME)


if __name__ == '__main__':
    main()
