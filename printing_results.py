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

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway

CLIENT_ID = 80
BAR_SIZE = '10m'
BAR_SECONDS = 5 * 60
CANDLE_PADDING = 10

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
    """Fetch 5-min OHLCV bars from IBKR covering earliest_dt-10 bars through now."""
    pad = datetime.timedelta(seconds=CANDLE_PADDING * BAR_SECONDS)
    now = datetime.datetime.now(datetime.timezone.utc)
    start = earliest_dt - pad
    duration_sec = int((now - start).total_seconds())
    if duration_sec > 86400:
        duration = f'{(duration_sec + 86399) // 86400} D'  # IBKR rejects >86400 S for 5-min bars
    else:
        duration = f'{duration_sec} S'

    contract = gw.make_stock_contract(symbol, currency=currency)
    bars = gw.fetch_historical(contract, duration=duration, bar_size=BAR_SIZE, use_rth=False)
    if not bars:
        logger.error('No data returned for %s', symbol)
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
            ts = trade['date']  # UTC-aware, matches tz-aware df index from ib_insync
            idx = df.index.searchsorted(ts)
            if idx >= len(df):
                continue
            prices.iloc[idx] = trade['price']
        if not prices.isna().all():
            addplots.append(
                mpf.make_addplot(prices, type='scatter', markersize=100, marker=marker, color=color)
            )
    return addplots


def main():
    ticker   = sys.argv[1] if len(sys.argv) > 1 else 'RKLB'
    currency = sys.argv[2] if len(sys.argv) > 2 else 'USD'
    filepath = Path('logs/trades_rklb_gluptasek_26Jun1.csv')

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
    plt.show()


if __name__ == '__main__':
    main()
