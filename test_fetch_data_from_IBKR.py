from ibkr import IBKRGateway
from rocket_janek import fetch_data_from_IBKR

CLIENT_ID = 103
SYMBOL = 'KLAC'
N_BARS = 10

gw = IBKRGateway(client_id=CLIENT_ID)
gw.ensure_connected()

df = fetch_data_from_IBKR(gw, SYMBOL, duration=f'{N_BARS * 1800} S', bar_size='30m', use_rth=True, currency='USD')

gw.disconnect()

print(df)
print()
print('df.iloc[-1]:')
print(df.iloc[-1])
print()
print('df.iloc[-2]:')
print(df.iloc[-2])

# df.iloc[-1]:
# Open        213.02
# High        213.18
# Low         212.20
# Close       212.20
# Volume    84764.00