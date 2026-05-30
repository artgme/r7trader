"""
CEX trading client using the ccxt library.

Currently configured for Kraken. To switch exchanges, replace ccxt.kraken()
with any other ccxt-supported exchange class.

Credentials are loaded from the .env file:
    R2TRADER_KRAKEN_API_KEY=your_api_key
    R2TRADER_KRAKEN_API_SECRET=your_private_key
"""

import os
import ccxt
from dotenv import load_dotenv

load_dotenv()


class CexTrader:
    def __init__(self) -> None:
        self.exchange: ccxt.Exchange | None = None

    def connect(self) -> bool:
        """
        Initialise the Kraken client and verify credentials by loading markets.
        Returns True on success, False if credentials are missing or invalid.
        """
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

    def get_balances(self) -> dict | None:
        """
        Fetch and display all non-zero spot balances.
        Returns a dict of {currency: total} or None if not connected.
        """
        if self.exchange is None:
            print("ERROR: Not connected. Call connect() first.")
            return None

        try:
            balance = self.exchange.fetch_balance()
        except ccxt.BaseError as e:
            print(f"ERROR: Failed to fetch balances: {e}")
            return None

        # filter out currencies with zero total balance
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


if __name__ == "__main__":
    trader = CexTrader()
    if trader.connect():
        trader.get_balances()
