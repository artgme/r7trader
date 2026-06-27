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
CANDLE_PADDING = 10
PLIK = 'logs/trades_spcx_gluptasek_26Jun1.csv'

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


# Usage: df = fetch_candles(gw, 'RKLB', earliest_dt, latest_dt, currency='USD')
def fetch_candles(gw: IBKRGateway, symbol: str, earliest_dt, latest_dt, currency: str = 'USD') -> pd.DataFrame | None:
    """Fetch 1-min OHLCV bars from IBKR covering earliest_dt-10 bars through latest_dt+10 bars."""
    pad = datetime.timedelta(seconds=CANDLE_PADDING * BAR_SECONDS)
    start = earliest_dt - pad
    end   = latest_dt  + pad
    duration_sec = int((end - start).total_seconds())
    if duration_sec > 86400:
        duration = f'{(duration_sec + 86399) // 86400} D'
    else:
        duration = f'{duration_sec} S'

    contract = gw.make_stock_contract(symbol, currency=currency)
    bars = gw.fetch_historical(contract, duration=duration, bar_size=BAR_SIZE, use_rth=True, end_dt=end)
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


# Usage: addplots = make_trade_addplots(df, trades)
def make_trade_addplots(df: pd.DataFrame, trades: list[dict]) -> list:
    """Build mplfinance addplot marker objects from trade list.
    Marker shapes: ^ enter_long (green), v enter_short (red), x exits (lime/orange)."""
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
            addplots.append(
                mpf.make_addplot(prices, type='scatter', markersize=100, marker=marker, color=color)
            )
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

    trades = read_trades(filepath, ticker)
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

    gw = IBKRGateway(client_id=CLIENT_ID)
    if not gw.ensure_connected():
        logger.error('Could not connect to IBKR.')
        return

    try:
        df = fetch_candles(gw, ticker, earliest, latest, currency=currency)
    finally:
        gw.disconnect()

    if df is None or df.empty:
        logger.error('No candle data — exiting.')
        return

    addplots = make_trade_addplots(df, trades)
    kwargs = dict(
        type='candle', volume=True,
        title=f'{ticker} — {earliest.date()}',
        style='charles', figsize=(14, 8), returnfig=True,
    )
    if addplots:
        kwargs['addplot'] = addplots
    fig, axes = mpf.plot(df, **kwargs)
    annotate_trades(axes[0], df, trades)
    plt.show()


if __name__ == '__main__':
    main()
