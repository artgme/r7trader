"""
data_loaders.py — functions that turn raw data files into BackTrader feeds.

TradingView CSV export
----------------------
On a chart click  Export chart data  →  CSV.
Required columns (case-insensitive): time, open, high, low, close[, volume]

The `time` column is handled in two formats automatically:
  - UNIX timestamp (integer seconds since epoch) — e.g. 1769620200
  - ISO datetime string, optionally with UTC offset — e.g. 2024-01-02 00:00:00+00:00

Any extra indicator columns exported by TradingView are ignored.
"""

import backtrader as bt
import pandas as pd


def _parse_time_column(series: pd.Series) -> pd.Series:
    """Return a tz-naive UTC datetime Series regardless of input format."""
    # If the column is already numeric (or looks numeric), treat as UNIX seconds
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit='s', utc=True).dt.tz_localize(None)

    # Try numeric conversion first (strings like "1769620200")
    numeric = pd.to_numeric(series, errors='coerce')
    if numeric.notna().all():
        return pd.to_datetime(numeric, unit='s', utc=True).dt.tz_localize(None)

    # Fall back to ISO datetime string (strip tz offset if present)
    return pd.to_datetime(series, utc=True).dt.tz_localize(None)


def load_tradingview_csv(
    filepath: str,
    timeframe: int = bt.TimeFrame.Days,
    compression: int = 1,
) -> bt.feeds.PandasData:
    """
    Return a BackTrader PandasData feed built from a TradingView CSV.

    Args:
        filepath    : path to the exported CSV file
        timeframe   : bt.TimeFrame constant (default: Days)
        compression : bars per unit, e.g. 1 for daily, 60 for 60-min
    """
    df = pd.read_csv(filepath)

    # Normalise column names (handles mixed case and leading/trailing spaces)
    df.columns = [c.strip().lower() for c in df.columns]

    # Parse time — works for both UNIX timestamps and ISO datetime strings
    df['time'] = _parse_time_column(df['time'])
    df = df.sort_values('time').set_index('time')

    required = {'open', 'high', 'low', 'close'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"CSV '{filepath}' is missing required columns: {missing}\n"
            f"Found: {list(df.columns)}"
        )

    if 'volume' not in df.columns:
        df['volume'] = 0.0

    # Cast to float to avoid object-dtype surprises
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')

    return bt.feeds.PandasData(
        dataname=df,
        timeframe=timeframe,
        compression=compression,
        open='open',
        high='high',
        low='low',
        close='close',
        volume='volume',
        openinterest=-1,    # no open-interest column
    )
