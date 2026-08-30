import time
from collections import deque
from zoneinfo import ZoneInfo

from ibkr import IBKRGateway

CLIENT_ID = 108
SYMBOLS = ['KLAC', 'AAPL']
MAXLEN = 12  # rolling window kept per symbol -- 12 x 5s bars = 1 minute; stand-in for a real vol_len
EXCHANGE_TZ = ZoneInfo('America/New_York')

# Exercises IBKRGateway.start_realtime_bars() / on_realtime_bar() / stop_realtime_bars() --
# the wrapper added around EClient.reqRealTimeBars() / EWrapper.realtimeBar() -- rather than
# calling ibapi directly, unlike test_streaming_market_data.py's raw reqMktData() test.
#
# One callback fires for every symbol's bars, distinguished only by reqId -- so this test
# accumulates into a dict of per-symbol deques, the same shape rocket_janek.py would need to
# maintain a live rolling window per symbol for check_vol_price_body().
gw = IBKRGateway(client_id=CLIENT_ID)
if not gw.ensure_connected():
    print('Could not connect to IBKR.')
    raise SystemExit(1)

symbol_by_req_id: dict[int, str] = {}
bars_by_symbol: dict[str, deque] = {symbol: deque(maxlen=MAXLEN) for symbol in SYMBOLS}


def _on_error(reqId, errorCode, errorString):
    if errorCode >= 2100:  # informational (farm/connection status), not a real problem
        print(f'INFO {errorCode} (reqId={reqId}): {errorString}')
    else:
        print(f'ERROR {errorCode} (reqId={reqId}): {errorString}')


def _on_bar(reqId, bar):
    symbol = symbol_by_req_id[reqId]
    bars_by_symbol[symbol].append(bar)

    local_time = bar.date.astimezone(EXCHANGE_TZ)
    print(f'{symbol:5s}  reqId={reqId}  {local_time:%Y-%m-%d %H:%M:%S}  '
          f'O={bar.open:.2f} H={bar.high:.2f} L={bar.low:.2f} C={bar.close:.2f} V={bar.volume:.0f}  '
          f'(buffer: {len(bars_by_symbol[symbol])}/{MAXLEN})')


gw.on_error(_on_error)
gw.on_realtime_bar(_on_bar)

for symbol in SYMBOLS:
    contract = gw.make_stock_contract(symbol)
    req_id = gw.start_realtime_bars(contract, what_to_show='TRADES', use_rth=True)
    symbol_by_req_id[req_id] = symbol
    print(f'Subscribed to 5s real-time bars for {symbol} (reqId={req_id}).')

print('Ctrl+C to stop.')
try:
    while True:
        time.sleep(1)
        # bars_by_symbol is also readable from here, e.g. once a symbol's buffer fills:
        # for symbol, bars in bars_by_symbol.items():
        #     if len(bars) == MAXLEN:
        #         ...  # build a DataFrame from `bars` and run check_vol_price_body()
except KeyboardInterrupt:
    pass
finally:
    for req_id in symbol_by_req_id:
        gw.stop_realtime_bars(req_id)
    gw.disconnect()
