import pandas as pd
import mplfinance as mpf
from ibkr import IBKRGateway

CLIENT_ID = 89
SYMBOL = 'EURUSD'  # Forex trades ~24/5, so it's tradable right now regardless of the US market clock

gw = IBKRGateway(client_id=CLIENT_ID)
gw.ensure_connected()

contract = gw.make_forex_contract(SYMBOL)
# Forex has no TRADES data on IBKR (no centralized tape) -- MIDPOINT is what's actually available.
bars = gw.fetch_historical(contract, duration='1 D', bar_size='30m', what_to_show='MIDPOINT', use_rth=False)

gw.disconnect()

df = pd.DataFrame([{
    'Date': b.date, 'Open': b.open, 'High': b.high,
    'Low': b.low, 'Close': b.close, 'Volume': b.volume,
} for b in bars])
df['Date'] = pd.to_datetime(df['Date'])
df.set_index('Date', inplace=True)

mpf.plot(df, type='candle', volume=True, title=f'{SYMBOL} - 30m', style='charles')
