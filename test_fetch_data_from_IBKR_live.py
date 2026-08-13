from ibkr import IBKRGateway

CLIENT_ID = 104
SYMBOL = 'KLAC'
N_BARS = 5

gw = IBKRGateway(client_id=CLIENT_ID)
gw.ensure_connected()

contract = gw.make_stock_contract(SYMBOL)
bars = gw.fetch_live_bars(contract, duration=f'{N_BARS * 600} S', bar_size='10m', use_rth=True)

def on_update(bars, has_new_bar):
    print(f'\n--- update (has_new_bar={has_new_bar}) ---', flush=True)
    last_n = bars[-N_BARS:]
    for i, b in enumerate(last_n):
        marker = '  <-- iloc[-1]' if i == len(last_n) - 1 else ''
        print(' ', b.date, b.open, b.high, b.low, b.close, b.volume, marker, flush=True)

bars.updateEvent += on_update
on_update(bars, False)  # print the initial snapshot immediately

print('\nListening for live updates (Ctrl+C to stop)...')
try:
    gw.ib.run()
except KeyboardInterrupt:
    pass
finally:
    gw.disconnect()
