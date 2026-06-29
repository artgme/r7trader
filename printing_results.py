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
BAR_SIZE = '1m'
BAR_SECONDS = 1 * 60  # candle size in minutes * 60 seconds
CANDLE_PADDING = 3
PLIK = 'logs/trades_rocket_janek_2905_03.csv'

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
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
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


# Usage: df = fetch_candles(gw, 'RKLB', earliest_dt, latest_dt, currency='USD', bar_size='10m', bar_seconds=600)
def fetch_candles(gw: IBKRGateway, symbol: str, earliest_dt, latest_dt, currency: str = 'USD', bar_size: str = BAR_SIZE, bar_seconds: int = BAR_SECONDS) -> pd.DataFrame | None:
    """Fetch OHLCV bars from IBKR covering earliest_dt-CANDLE_PADDING bars through latest_dt+CANDLE_PADDING bars."""
    pad = datetime.timedelta(seconds=CANDLE_PADDING * bar_seconds)
    start = earliest_dt - pad
    end   = latest_dt  + pad
    duration_sec = int((end - start).total_seconds())
    if duration_sec > 86400:
        duration = f'{(duration_sec + 86399) // 86400} D'
    else:
        duration = f'{duration_sec} S'

    contract = gw.make_stock_contract(symbol, currency=currency)
    bars = gw.fetch_historical(contract, duration=duration, bar_size=bar_size, use_rth=True, end_dt=end)
    if not bars:
        logger.error('No data returned for %s', symbol)
        return None

    df = pd.DataFrame([{
        'Date': b.date, 'Open': b.open, 'High': b.high,
        'Low': b.low, 'Close': b.close, 'Volume': b.volume,
    } for b in bars])
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    # ib_insync (formatDate=1) returns tz-naive ET times; make them tz-aware
    # so _bar_idx can compare correctly against UTC-aware CSV timestamps.
    if df.index.tz is None:
        df.index = df.index.tz_localize(ZoneInfo('America/New_York'))
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


def main():
    ticker   = sys.argv[1] if len(sys.argv) > 1 else 'RKLB'
    currency = sys.argv[2] if len(sys.argv) > 2 else 'USD'
    filepath = Path(PLIK)

    try:
        trades = read_trades(filepath, ticker)
    except FileNotFoundError as e:
        logger.error('%s', e)
        return
    if not trades:
        logger.error('No trades found for %s in %s', ticker, filepath)
        return

    trades.sort(key=lambda t: t['date'])
    round_trips = calc_pnl(trades)

    logger.info('Found %d trade(s) for %s, %d completed round-trips:', len(trades), ticker, len(round_trips))
    for i, rt in enumerate(round_trips, 1):
        e, x = rt['entry'], rt['exit']
        print(f"  #{i:2d}  {e['action']:12s} @ {e['price']:8.2f}  →  {x['action']:17s} @ {x['price']:8.2f}  "
              f"P&L: {rt['pnl']:+7.2f}  cumulative: {rt['cumulative']:+7.2f}")
    total = round_trips[-1]['cumulative'] if round_trips else 0.0
    print(f"  TOTAL P&L: {total:+.2f}")

    earliest = min(t['date'] for t in trades)
    latest   = max(t['date'] for t in trades)
    entry_trades = [t for t in trades if t['action'].startswith('enter')]
    exit_trades  = [t for t in trades if t['action'].startswith('exit')]

    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        logger.error('Could not connect to IBKR.')
        return

    try:
        df_10m = fetch_candles(gw, ticker, earliest, latest, currency=currency,
                               bar_size='10m', bar_seconds=10 * 60)
        df_1m  = fetch_candles(gw, ticker, earliest, latest, currency=currency,
                               bar_size='1m', bar_seconds=10 * 60)
    finally:
        gw.disconnect()

    if df_10m is None or df_10m.empty:
        logger.error('No 10m candle data — exiting.')
        return
    if df_1m is None or df_1m.empty:
        logger.error('No 1m candle data — exiting.')
        return

    fig = plt.figure(figsize=(14, 14))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1, 3, 1], hspace=0.4)
    ax_10m     = fig.add_subplot(gs[0])
    ax_10m_vol = fig.add_subplot(gs[1], sharex=ax_10m)
    ax_1m      = fig.add_subplot(gs[2])
    ax_1m_vol  = fig.add_subplot(gs[3], sharex=ax_1m)
    fig.suptitle(f'{ticker} — {earliest.date()}')

    # Top subplot: 10m candles with entry markers
    entry_addplots = make_trade_addplots(df_10m, entry_trades, ax=ax_10m)
    mpf_kwargs = dict(type='candle', ax=ax_10m, volume=ax_10m_vol, style='charles')
    if entry_addplots:
        mpf_kwargs['addplot'] = entry_addplots
    mpf.plot(df_10m, **mpf_kwargs)
    ax_10m.set_title('10m — entries')
    annotate_trades(ax_10m, df_10m, entry_trades)

    # Bottom subplot: 1m candles with exit markers
    exit_addplots = make_trade_addplots(df_1m, exit_trades, ax=ax_1m)
    mpf_kwargs = dict(type='candle', ax=ax_1m, volume=ax_1m_vol, style='charles')
    if exit_addplots:
        mpf_kwargs['addplot'] = exit_addplots
    mpf.plot(df_1m, **mpf_kwargs)
    ax_1m.set_title('1m — exits')
    annotate_trades(ax_1m, df_1m, exit_trades)

    for ax in (ax_10m, ax_10m_vol, ax_1m, ax_1m_vol):
        ax.grid(axis='x', color='gray', linestyle='--', alpha=0.4, linewidth=0.5)

    # Synchronize x-axes by time — mplfinance uses integer bar positions internally,
    # so we convert bar index → timestamp → bar index in the other chart.
    _syncing = [False]

    def _sync_to_1m(_):
        if _syncing[0]: return
        _syncing[0] = True
        try:
            xmin, xmax = ax_10m.get_xlim()
            i_min = max(0, min(int(xmin), len(df_10m) - 1))
            i_max = max(0, min(int(xmax), len(df_10m) - 1))
            t_min = df_10m.index[i_min].tz_convert(df_1m.index.tz)
            t_max = df_10m.index[i_max].tz_convert(df_1m.index.tz)
            ax_1m.set_xlim(float(df_1m.index.searchsorted(t_min)) - 0.5,
                           float(df_1m.index.searchsorted(t_max)) + 0.5)
        finally:
            _syncing[0] = False

    def _sync_to_10m(_):
        if _syncing[0]: return
        _syncing[0] = True
        try:
            xmin, xmax = ax_1m.get_xlim()
            i_min = max(0, min(int(xmin), len(df_1m) - 1))
            i_max = max(0, min(int(xmax), len(df_1m) - 1))
            t_min = df_1m.index[i_min].tz_convert(df_10m.index.tz)
            t_max = df_1m.index[i_max].tz_convert(df_10m.index.tz)
            ax_10m.set_xlim(float(df_10m.index.searchsorted(t_min)) - 0.5,
                            float(df_10m.index.searchsorted(t_max)) + 0.5)
        finally:
            _syncing[0] = False

    ax_10m.callbacks.connect('xlim_changed', _sync_to_1m)
    ax_1m.callbacks.connect('xlim_changed', _sync_to_10m)

    _sync_to_1m(None)  # initial sync so both charts start aligned

    plt.tight_layout()
    plt.show()


if __name__ == '__main__':
    main()
