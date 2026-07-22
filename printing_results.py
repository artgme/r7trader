import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ib_insync').setLevel(logging.WARNING)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

import sys
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway

CLIENT_ID = 80
PLIK = 'logs/trades_20260722_0145_30m.csv'
FETCH_AND_PLOT = 1
TIMEFRAME = '30m'  # big-candle chart timeframe, e.g. '5m', '10m', '30m', '1h'
EXCHANGE_TZ = ZoneInfo('America/New_York')

# action → (marker, color)
MARKER_STYLE = {
    'enter_long':       ('^', 'green'),
    'enter_short':      ('v', 'red'),
    'exit_long_trail':  ('x', 'lime'),
    'exit_short_trail': ('x', 'orange'),
}


# Usage: trades = read_trades(Path('logs/trades.csv'), 'RKLB')
def read_trades(filepath: Path, ticker: str) -> list[dict]:
    """Read trade log CSV, deduplicate, filter to ticker. Returns list of dicts with
    keys: date, ticker, action, price, size."""
    if not Path(filepath).exists():
        raise FileNotFoundError(f"Trade log not found: {filepath}")
    df = pd.read_csv(filepath)
    df = df.drop_duplicates()
    df = df[df['symbol'] == ticker].copy()
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(EXCHANGE_TZ)
    return [
        {
            'date':   row['timestamp'],
            'ticker': row['symbol'],
            'action': row['action'],
            'price':  float(row['price']),
            'size':   float(row['size']),
        }
        for _, row in df.iterrows()
    ]


# Usage: _parse_timeframe('30m') -> Timedelta('0 days 00:30:00')
def _parse_timeframe(tf: str) -> pd.Timedelta:
    if tf.endswith('m'):
        return pd.Timedelta(minutes=int(tf[:-1]))
    if tf.endswith('h'):
        return pd.Timedelta(hours=int(tf[:-1]))
    raise ValueError(f'Unsupported timeframe: {tf}')


# Usage: df = fetch_day(gw, date(2026, 6, 29), 'RKLB', 'USD', '10m')
def fetch_day(gw: IBKRGateway, date: datetime.date, ticker: str, currency: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch the full RTH session for `date`."""
    end_dt = datetime.datetime.combine(date, datetime.time(23, 59, 59), tzinfo=EXCHANGE_TZ)
    contract = gw.make_stock_contract(ticker, currency=currency)
    bars = gw.fetch_historical(contract, duration='1 D', bar_size=timeframe, use_rth=True, end_dt=end_dt)
    if not bars:
        logger.error('No data returned for %s', ticker)
        return None

    df = pd.DataFrame([{
        'Date': b.date, 'Open': b.open, 'High': b.high,
        'Low': b.low, 'Close': b.close, 'Volume': b.volume,
    } for b in bars])
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    return df


# Usage: round_trips = calc_pnl(trades)
def calc_pnl(trades: list[dict]) -> list[dict]:
    """Match entries to exits FIFO, return one dict per completed round trip.
    Each dict: entry, exit, pnl, cumulative. Unmatched entries are ignored."""
    open_entries = []
    result = []
    cumulative = 0.0
    for t in trades:
        if t['action'].startswith('enter'):
            open_entries.append(t)
        elif t['action'].startswith('exit') and open_entries:
            entry = open_entries.pop(0)
            if 'short' in t['action']:
                pnl = (entry['price'] - t['price']) * t['size']
            else:
                pnl = (t['price'] - entry['price']) * t['size']
            cumulative += pnl
            result.append({'entry': entry, 'exit': t, 'pnl': pnl, 'cumulative': cumulative})
    return result


# Usage: pnl = calc_all_pnl(Path('logs/trades.csv'))
def calc_all_pnl(filepath: Path) -> dict:
    """Return {symbol: cumulative_pnl} for every ticker in the trade log."""
    df = pd.read_csv(filepath).drop_duplicates()
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(EXCHANGE_TZ)
    result = {}
    for symbol, group in df.groupby('symbol'):
        trades = [
            {'date': row['timestamp'], 'ticker': symbol,
             'action': row['action'], 'price': float(row['price']), 'size': float(row['size'])}
            for _, row in group.sort_values('timestamp').iterrows()
        ]
        trips = calc_pnl(trades)
        result[symbol] = trips[-1]['cumulative'] if trips else 0.0
    return result


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


def fetch_and_plot(ticker: str, currency: str, trades: list[dict], day: datetime.date, timeframe: str = TIMEFRAME) -> None:
    entry_trades = [t for t in trades if t['action'].startswith('enter')]
    exit_trades  = [t for t in trades if t['action'].startswith('exit')]

    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        logger.error('Could not connect to IBKR.')
        return

    try:
        df_big = fetch_day(gw, day, ticker, currency, timeframe)
        df_1m  = fetch_day(gw, day, ticker, currency, '1m')
    finally:
        gw.disconnect()

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
    fig.suptitle(f'{ticker} — {day}')

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
    ticker   = sys.argv[1] if len(sys.argv) > 1 else 'RKLB'
    currency = sys.argv[2] if len(sys.argv) > 2 else 'USD'
    filepath = Path(PLIK)

    try:
        trades = read_trades(filepath, ticker)
    except FileNotFoundError as e:
        logger.error('%s', e)
        return

    if trades:
        trades.sort(key=lambda t: t['date'])
        round_trips = calc_pnl(trades)

        logger.info('Found %d trade(s) for %s, %d completed round-trips:', len(trades), ticker, len(round_trips))
        for i, rt in enumerate(round_trips, 1):
            e, x = rt['entry'], rt['exit']
            print(f"  #{i:2d}  {e['action']:12s} @ {e['price']:8.2f}  →  {x['action']:17s} @ {x['price']:8.2f}  "
                  f"P&L: {rt['pnl']:+7.2f}  cumulative: {rt['cumulative']:+7.2f}")
        total = round_trips[-1]['cumulative'] if round_trips else 0.0
        print(f"  TOTAL P&L: {total:+.2f}")
    else:
        logger.info('No trades found for %s in %s.', ticker, filepath)

    all_pnl = calc_all_pnl(filepath)
    print(f'\n--- All tickers ({filepath.name}) ---')
    for sym, pnl in all_pnl.items():
        print(f"  {sym:8s}  {pnl:+.2f}")
    print(f"  {'TOTAL':8s}  {sum(all_pnl.values()):+.2f}")

    if trades and FETCH_AND_PLOT:
        first_line = pd.read_csv(filepath, nrows=1)
        day = pd.to_datetime(first_line['timestamp'].iloc[0]).date()
        fetch_and_plot(ticker, currency, trades, day)


if __name__ == '__main__':
    main()
