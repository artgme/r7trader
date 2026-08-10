import datetime
import logging
import threading

import pandas as pd
from flask import Flask

from logging_functions import EXCHANGE_TZ

logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

POSITION_COLUMNS = ['Number', 'Entry Time', 'Direction', 'Currency', 'Entry Price', 'Current Price', 'PL', 'Contract']

_lock = threading.Lock()
positions_df = pd.DataFrame(columns=POSITION_COLUMNS)
positions_df.index.name = 'Ticker'


def _pl(number: float, direction: str, entry_price: float, current_price: float) -> float:
    sign = 1 if direction == 'BUY' else -1
    return (current_price - entry_price) * number * sign


# `contract` is the fully-qualified ib_insync Contract for this position (carries currency,
# exchange, conId, ...) so it can be handed straight to gw.close_position() later without
# having to reconstruct or guess it. Currency is read off it rather than passed separately.
def add_position(ticker: str, number: float, direction: str, entry_price: float, contract) -> None:
    with _lock:
        positions_df.loc[ticker] = [number, datetime.datetime.now(EXCHANGE_TZ), direction, contract.currency, entry_price, entry_price, 0.0, contract]


def remove_position(ticker: str) -> None:
    with _lock:
        positions_df.drop(index=ticker, errors='ignore', inplace=True)


def update_current_price(ticker: str, price: float) -> None:
    with _lock:
        if ticker in positions_df.index:
            row = positions_df.loc[ticker]
            positions_df.loc[ticker, 'Current Price'] = price
            positions_df.loc[ticker, 'PL'] = _pl(row['Number'], row['Direction'], row['Entry Price'], price)


# Reconciles positions_df against IBKR's own bookkeeping: adds rows for open positions we
# aren't tracking yet (pre-existing positions, or ones opened before this process started —
# Entry Time is unknown for these, so it's left as NaT and Entry Price falls back to avgCost)
# and drops rows for tickers IBKR no longer reports as open (e.g. a trail/TP filled).
# Pass the list returned by gw.get_positions() — call once per loop pass to keep the dashboard honest.
def sync_with_ibkr(ibkr_positions: list) -> None:
    open_tickers = set()
    with _lock:
        for p in ibkr_positions:
            if p.position == 0:
                continue
            ticker = p.contract.symbol
            open_tickers.add(ticker)
            if ticker not in positions_df.index:
                direction = 'BUY' if p.position > 0 else 'SELL'
                positions_df.loc[ticker] = [abs(p.position), pd.NaT, direction, p.contract.currency, p.avgCost, p.avgCost, 0.0, p.contract]

        stale = positions_df.index.difference(open_tickers)
        positions_df.drop(index=stale, inplace=True)


app = Flask(__name__)


@app.route('/')
def dashboard():
    with _lock:
        display_df = positions_df.drop(columns='Contract')
        table_html = display_df.to_html(classes='positions', border=0) if not display_df.empty else None
    return f"""
    <html>
      <head>
        <title>Positions</title>
        <meta http-equiv="refresh" content="5">
        <style>
          body {{ font-family: sans-serif; margin: 2rem; }}
          table.positions {{ border-collapse: collapse; width: 100%; }}
          table.positions th, table.positions td {{ padding: 6px 12px; border-bottom: 1px solid #ddd; text-align: right; }}
          table.positions th {{ text-align: right; background: #f5f5f5; }}
          table.positions td:first-child, table.positions th:first-child {{ text-align: left; }}
        </style>
      </head>
      <body>
        <h2>Open Positions</h2>
        {table_html or '<p>No open positions.</p>'}
      </body>
    </html>
    """


def start_dashboard(host: str = '127.0.0.1', port: int = 8000) -> None:
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True),
        daemon=True,
    )
    thread.start()
    logger.info('Positions dashboard running at http://%s:%s', host, port)
