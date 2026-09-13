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

from common import timeframe_to_seconds, RED, GREEN, WHITE, RESET
from signal_checks import check_vol_price_body, check_vol_price_body_dir, scan_trailing_stop, scan_take_profit
import configs_rocketJanek as cfg

load_dotenv()
ALPACA_API_KEY = os.environ.get('ALPACA_API_KEY')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_API_SECRET')

TICKER = 'ALAB'
CURRENCY = 'USD'
TIMEFRAME = '10m'
START_DT = datetime.datetime(2026, 6, 1, 9, 30, tzinfo=ZoneInfo('America/New_York'))
END_DAY = datetime.date(2026, 9, 4)
QUANTITY = 10
FETCH_AND_PLOT = 1

CLOSE_BEFORE_SECONDS = 1200  # how far ahead of RTH_CLOSE to force-close a trade still open at
                              # end of session — mirrors rocket_janek.py's CLOSE_OVERNIGHT

EXCHANGE_TZ = ZoneInfo('America/New_York')
RTH_OPEN = datetime.time(9, 30)
RTH_CLOSE = datetime.time(16, 0)

# action → (marker, color) — shape encodes *why* the trade exited, color encodes direction
# (lime = long, orange = short), so the plot reads at a glance without a legend lookup.
MARKER_STYLE = {
    'enter_long':       ('^', 'green'),
    'enter_short':      ('v', 'red'),
    'exit_long_trail':  ('x', 'lime'),
    'exit_short_trail': ('x', 'orange'),
    'exit_long_tp':     ('*', 'lime'),
    'exit_short_tp':    ('*', 'orange'),
    'exit_long_eod':    ('s', 'lime'),
    'exit_short_eod':   ('s', 'orange'),
}

# trade['exit_reason'] -> the MARKER_STYLE suffix for that exit
_EXIT_REASON_SUFFIX = {
    'trail_stop':    'trail',
    'take_profit':   'tp',
    'session_close': 'eod',
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
    duration limit like IBKR), normalized to the same shape signal_checks.check_vol_price_body() expects:
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


# Usage: marker_trades = to_marker_trades(trades)
def to_marker_trades(trades: list[dict]) -> list[dict]:
    """Convert run_backtest()'s trade dicts into the enter/exit marker format fetch_and_plot()
    expects. The exit marker's shape reflects exit_reason (trail_stop / take_profit /
    session_close), defaulting to 'trail' for any older trade dict that predates that field."""
    marker_trades = []
    for t in trades:
        side = 'long' if t['direction'] == 'long' else 'short'
        marker_trades.append({'date': t['entry_time'], 'action': f'enter_{side}', 'price': t['entry_price']})
        if t['exit_time'] is not None:
            suffix = _EXIT_REASON_SUFFIX.get(t.get('exit_reason'), 'trail')
            marker_trades.append({'date': t['exit_time'], 'action': f'exit_{side}_{suffix}', 'price': t['exit_price']})
    return marker_trades


# Usage: cutoff = _session_close_cutoff(entry_time)
def _session_close_cutoff(entry_time) -> datetime.datetime:
    """15:40 ET (RTH_CLOSE - CLOSE_BEFORE_SECONDS) on entry_time's calendar day. Mirrors
    rocket_janek.py's CLOSE_OVERNIGHT cutoff."""
    close_dt = datetime.datetime.combine(entry_time.date(), RTH_CLOSE, tzinfo=EXCHANGE_TZ)
    return close_dt - datetime.timedelta(seconds=CLOSE_BEFORE_SECONDS)


# Usage: trades, checks = run_backtest(symbol, low_df, high_df, start_dt, timeframe, vol_len,
#                                       vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, quantity)
# Usage (directional): trades, checks = run_backtest(symbol, low_df, high_df, start_dt, timeframe, vol_len,
#                                       0, 0, 0, 0, quantity, 0, long_params=long_params, short_params=short_params)
def run_backtest(symbol: str, low_df: pd.DataFrame, high_df: pd.DataFrame, start_dt, timeframe: str,
                  vol_len: int, vol_multiplier: float, price_move_pct: float, trail_stop_pct: float,
                  body_ratio_threshold: float, quantity: float,
                  take_profit_pct: float, long_params: dict = None, short_params: dict = None) -> tuple[list[dict], list[dict]]:
    """Walk low_df candle-by-candle, calling check_vol_price_body() on each closed candle while flat —
    same window shape as the live loop (iloc[-2] = signal candle, iloc[-1] = next candle,
    standing in for the still-forming candle a live fetch would see). On a signal, fills at
    the next available 1m price, then scans high_df for whichever of the trailing stop or the
    take-profit target is hit first (bounded to the same trading session — see
    _session_close_cutoff), force-closing at the session cutoff if neither fires.

    Pass both long_params and short_params (each {'vol_multiplier', 'price_move_pct',
    'body_ratio_threshold', 'trail_stop_pct', 'take_profit_pct'}) to tune/trade long and short
    with independent entry and exit parameters via check_vol_price_body_dir() — the flat
    vol_multiplier/price_move_pct/trail_stop_pct/body_ratio_threshold/take_profit_pct args are then
    ignored. Leave both None (default) for today's behavior: one shared parameter set for both
    directions via check_vol_price_body(), unchanged.

    Returns (trades, checks) — checks records every candle evaluated while flat, signal or not,
    so a day with zero trades still shows why nothing fired."""
    directional = long_params is not None and short_params is not None
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

        # Same window shape check_vol_price_body() expects live: vol_len bars, candle i is iloc[-2]
        # (the signal candle), candle i+1 stands in for the still-forming iloc[-1] candle.
        window = low_df.iloc[i - vol_len + 2: i + 2].copy() #check_vol_price_body reads teh signal candle from a fixed position in the window iloc[-2]
        if directional:
            signal, _, debug, flags = check_vol_price_body_dir(window, long_params, short_params)
            checks.append({
                'symbol': symbol,
                'signal_time': low_df.index[i],
                'signal': signal or 'none',
                'volume': debug['volume'],
                'mean_volume': debug['mean_volume'],
                'current_pct': debug['current_pct'],
                'price_threshold_long': debug['price_threshold_long'],
                'price_threshold_short': debug['price_threshold_short'],
                'body_ratio': debug['body_ratio'],
                'green_volume_long': flags[0], 'green_volume_short': flags[1],
                'green_price': flags[2], 'red_price': flags[3],
                'green_body_long': flags[4], 'green_body_short': flags[5],
            })
        else:
            signal, _, trail_stop_loss, debug, flags = check_vol_price_body(window, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold)
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

        # Once direction is known, resolve the exit parameters that actually apply — the
        # direction-specific dict in directional mode, or today's flat shared values otherwise.
        if directional:
            p = long_params if direction == 'long' else short_params
            trail_stop_loss = p['trail_stop_pct']
            take_profit_pct = p['take_profit_pct']

        # Bound the scan to this trading session only, so a trade that never hits its stop or
        # target doesn't ride into the next day (previously it could scan past session boundaries
        # indefinitely, and if it never resolved before high_df ran out the whole backtest loop
        # would just stop — see the `break` this replaces below). Mirrors rocket_janek.py's
        # CLOSE_OVERNIGHT, which flattens any still-open live position at the same cutoff.
        session_cutoff = _session_close_cutoff(entry_time)
        window = high_df[(high_df.index >= entry_time) & (high_df.index <= session_cutoff)]

        # Scan for both exits over the same bounded window and take whichever actually happens
        # first, by time. This differs from the live tick-by-tick callback, which can just check
        # take-profit before the trailing stop every tick (each tick only spans ~1 minute, so
        # "checked first" and "happened first" are almost always the same thing there). Here we're
        # scanning a whole multi-hour window in one shot, so that shortcut doesn't hold — we have
        # to compare the two candidate exit times directly to find the true first-in-time exit.
        _, sl_time, sl_price = scan_trailing_stop(window, entry_time, entry_price, direction, trail_stop_loss)
        tp_time, tp_price = scan_take_profit(window, entry_time, entry_price, direction, take_profit_pct)

        if sl_price is not None and (tp_price is None or sl_time <= tp_time):
            exit_time, exit_price, exit_reason = sl_time, sl_price, 'trail_stop'
        elif tp_price is not None:
            exit_time, exit_price, exit_reason = tp_time, tp_price, 'take_profit'
        else:
            # Neither fired within the session — force-close at the last available bar at/before
            # the cutoff (its Close), same as rocket_janek.py's CLOSE_OVERNIGHT would.
            eod_bars = window[window.index <= session_cutoff]
            if eod_bars.empty:
                # Entry itself landed after the cutoff (a signal very late in the session) —
                # nothing left to hold, so it's flattened immediately at the entry price itself.
                exit_time, exit_price, exit_reason = entry_time, entry_price, 'session_close'
            else:
                exit_time, exit_price, exit_reason = eod_bars.index[-1], eod_bars.iloc[-1]['Close'], 'session_close'

        pnl = None
        if exit_price is not None:
            pnl = (exit_price - entry_price) * quantity if direction == 'long' else (entry_price - exit_price) * quantity

        trades.append({
            'symbol': symbol,
            'direction': direction,
            'signal_time': low_df.index[i],
            'entry_time': entry_time,
            'entry_price': entry_price,
            'trail_stop_pct': trail_stop_loss,
            'take_profit_pct': take_profit_pct,
            'exit_time': exit_time,
            'exit_price': exit_price,
            'exit_reason': exit_reason,
            'quantity': quantity,
            'pnl': pnl,
        })

        if exit_time is None:
            # Shouldn't happen given the session-close fallback above — guard against silently
            # truncating the whole backtest on one unresolved trade, which is the bug this
            # session-close logic exists to fix in the first place.
            i += 1
            continue

        if exit_reason == 'session_close':
            # A forced end-of-session close means we're done trading for the *rest of this day*
            # too (e.g. 15:40-16:00) — resuming with searchsorted() would just pick right back up
            # in that closing window and could open a same-day follow-on trade seconds after we
            # just flattened for the night. Skip straight to the next day that actually has bars
            # (naturally skipping weekends/holidays, since we're indexing into low_df itself).
            later_days = low_df.index[low_df.index.date > exit_time.date()]
            if later_days.empty:
                break  # that was the last day of data — nothing left to scan
            i = low_df.index.get_loc(later_days[0])
        else:
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


# Usage: printing_trades(TICKER, START_DT, END_DAY, TIMEFRAME, trades)
def printing_trades(ticker: str, start_dt, end_day, timeframe: str, trades: list[dict]) -> None:
    print(f'\n{ticker} backtest (Alpaca data): {start_dt} to {end_day}, {timeframe} signal / 1m exit, {len(trades)} trade(s)')
    print(f"\n  {'#':>3}  {'direction':9}  {'signal_time':25}  {'entry_time':25}  {'entry_price':>11}  {'exit_time':25}  {'exit_price':>10}  {'exit_reason':14}  {'trail_stop_pct':>15}  {'take_profit_pct':>16}  {'pnl':>10}  {'cum':>10}")
    cumulative = 0.0
    for i, t in enumerate(trades, 1):
        # Pad the plain text to fixed width first, then wrap in color — ANSI codes would
        # otherwise count toward the f-string width and break column alignment.
        exit_time_str = str(t['exit_time']) if t['exit_time'] is not None else 'OPEN (never exited)'
        exit_price_str = f"{t['exit_price']:>10.2f}" if t['exit_price'] is not None else f"{'n/a':>10}"
        exit_reason_str = t.get('exit_reason') or 'n/a'
        if t['pnl'] is not None:
            cumulative += t['pnl']
            pnl_color = GREEN if t['pnl'] >= 0 else RED
            pnl_str = f"{pnl_color}{t['pnl']:>+10.2f}{RESET}"
            cum_color = GREEN if cumulative >= 0 else RED
            cum_str = f"{cum_color}{cumulative:>+10.2f}{RESET}"
        else:
            pnl_str = f"{'n/a':>10}"
            cum_str = f"{'n/a':>10}"
        print(f"  {i:>3}  {t['direction']:9}  {str(t['signal_time']):25}  {str(t['entry_time']):25}  "
              f"{t['entry_price']:>11.2f}  {exit_time_str:25}  {exit_price_str}  {exit_reason_str:14}  "
              f"{t['trail_stop_pct']:>14.2f}%  {t['take_profit_pct']:>15.2f}%  {pnl_str}  {cum_str}")


# Usage: printing_checks(checks)
def printing_checks(checks: list[dict]) -> None:
    """Every candle evaluated while flat, whether or not it fired — shows why a signal didn't
    trigger just as clearly as why one did. Handles both run_backtest() shapes: shared-params
    checks (one price_threshold/trail_stop_pct) and directional checks (long/short each)."""
    directional = bool(checks) and 'price_threshold_long' in checks[0]
    if directional:
        print(f"\n  {'#':>3}  {'signal_time':25}  {'signal':6}  {'volume':>10}  {'mean_volume':>12}  {'current_pct':>12}  "
              f"{'price_thr_long':>15}  {'price_thr_short':>16}  {'body_ratio':>11}")
        for i, c in enumerate(checks, 1):
            volume_color = GREEN if (c['green_volume_long'] or c['green_volume_short']) else WHITE
            volume_str = f"{volume_color}{c['volume']:>10.0f}{RESET}"
            pct_color = GREEN if c['green_price'] else RED if c['red_price'] else WHITE
            pct_str = f"{pct_color}{c['current_pct']:>+11.2f}%{RESET}"
            body_color = GREEN if (c['green_body_long'] or c['green_body_short']) else WHITE
            body_str = f"{body_color}{c['body_ratio']:>11.2f}{RESET}"
            print(f"  {i:>3}  {str(c['signal_time']):25}  {c['signal']:6}  "
                  f"{volume_str}  {c['mean_volume']:>12.0f}  {pct_str}  "
                  f"{c['price_threshold_long']:>14.2f}%  {c['price_threshold_short']:>15.2f}%  {body_str}")
        return

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


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.error('Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env before running this.')
        return
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    fetch_start_day = START_DT.date() - datetime.timedelta(days=5)  # extra lookback so vol_len has history
    low_df = fetch_range(client, TICKER, fetch_start_day, END_DAY, TIMEFRAME)
    print(low_df)
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
    vol_len = 5
    vol_multiplier = 3.0
    price_move_pct = 3.0
    trail_stop_pct = 2.0
    body_ratio_threshold = 0.7
    take_profit_pct = 3.0

    trades, checks = run_backtest(TICKER, low_df, high_df, START_DT, TIMEFRAME, vol_len,
                                   vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold, QUANTITY, take_profit_pct=take_profit_pct)

    printing_trades(TICKER, START_DT, END_DAY, TIMEFRAME, trades)
    #printing_checks(checks)

    if trades and FETCH_AND_PLOT:
        fetch_and_plot(client, TICKER, to_marker_trades(trades), START_DT.date(), END_DAY, TIMEFRAME)


if __name__ == '__main__':
    main()
