"""
CEX trading client using the ccxt library.

Currently configured for Kraken. To switch exchanges, replace ccxt.kraken()
with any other ccxt-supported exchange class.

Credentials are loaded from the .env file:
    R2TRADER_KRAKEN_API_KEY=your_api_key
    R2TRADER_KRAKEN_API_SECRET=your_private_key
"""

import os
import threading
from datetime import datetime

import ccxt
from dotenv import load_dotenv

load_dotenv()


class CexTrader:
    def __init__(self) -> None:
        self.exchange: ccxt.Exchange | None = None

    # Initialise the Kraken client with API credentials from .env and verify the
    # connection by loading all available markets.  Returns True on success, False
    # if credentials are missing, invalid, or the network is unreachable.
    def connect(self) -> bool:
        api_key = os.getenv("R2TRADER_KRAKEN_API_KEY")
        api_secret = os.getenv("R2TRADER_KRAKEN_API_SECRET")

        if not api_key or not api_secret:
            print("ERROR: R2TRADER_KRAKEN_API_KEY and R2TRADER_KRAKEN_API_SECRET not found in .env")
            return False

        self.exchange = ccxt.kraken({
            "apiKey": api_key,
            "secret": api_secret,
        })

        try:
            # load_markets() fetches all trading pairs and confirms the connection.
            self.exchange.load_markets()
            print(f"Connected to Kraken ({len(self.exchange.markets)} markets loaded).")
            return True
        except ccxt.AuthenticationError:
            print("ERROR: Invalid Kraken API key or secret.")
            return False
        except ccxt.NetworkError as e:
            print(f"ERROR: Network error while connecting to Kraken: {e}")
            return False

    # Fetch and print all non-zero spot balances for the authenticated account.
    # Returns a dict of {currency: total} or None if not connected.
    def get_balances(self) -> dict | None:
        if self.exchange is None:
            print("ERROR: Not connected. Call connect() first.")
            return None

        try:
            balance = self.exchange.fetch_balance()
        except ccxt.BaseError as e:
            print(f"ERROR: Failed to fetch balances: {e}")
            return None

        non_zero = {
            currency: amount
            for currency, amount in balance["total"].items()
            if amount > 0
        }

        if non_zero:
            print("\nBalances:")
            for currency, total in non_zero.items():
                print(f"  {currency}: {total}")
        else:
            print("No open balances.")

        return non_zero

    # Download historical OHLCV bars for `symbol` (e.g. 'BTC/USD') at the given
    # `timeframe` (e.g. '1m', '5m', '1h', '1d').  `since` is an optional datetime
    # marking the start of the range; omitting it lets the exchange choose the
    # default window.  `limit` caps the number of returned bars (Kraken max: 720).
    # Returns a list of [timestamp_ms, open, high, low, close, volume] lists,
    # or None if not connected or a network/API error occurs.
    def fetch_historical_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[list] | None:
        if self.exchange is None:
            print("ERROR: Not connected. Call connect() first.")
            return None

        since_ms = int(since.timestamp() * 1000) if since else None

        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
            print(f"Fetched {len(bars)} historical bars for {symbol} [{timeframe}].")
            return bars
        except ccxt.BadSymbol:
            print(f"ERROR: Symbol '{symbol}' not available on Kraken.")
            return None
        except ccxt.BaseError as e:
            print(f"ERROR: Failed to fetch historical OHLCV: {e}")
            return None

    # Poll the exchange for new OHLCV bars in a blocking loop until `stop_event` is set.
    # Calls `callback(bar)` for each bar where `bar` is [ts_ms, open, high, low, close, volume].
    # On start-up, the last `seed_bars` completed bars are delivered immediately so the
    # caller has data to display before live ticks arrive.  `poll_interval_s` controls
    # how often the exchange is queried; 5 s is a safe default for 1-minute bars.
    def stream_live_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        callback,
        stop_event: threading.Event,
        seed_bars: int = 5,
        poll_interval_s: int = 5,
    ) -> None:
        if self.exchange is None:
            print("ERROR: Not connected. Call connect() first.")
            return

        last_ts: int | None = None

        while not stop_event.is_set():
            try:
                # fetch a small window; the first pass seeds the chart with recent bars
                fetch_limit = seed_bars if last_ts is None else 3
                bars = self.exchange.fetch_ohlcv(symbol, timeframe, limit=fetch_limit)
                for bar in bars:
                    if last_ts is None or bar[0] > last_ts:
                        callback(bar)
                        last_ts = bar[0]
            except ccxt.BaseError as e:
                print(f"ERROR: Live stream fetch failed: {e}")

            stop_event.wait(poll_interval_s)


if __name__ == "__main__":
    trader = CexTrader()
    if trader.connect():
        trader.get_balances()
