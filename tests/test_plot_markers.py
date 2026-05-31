"""
Marker rendering test for DashPlotter.

Posts all 12 trade marker types to the live chart already running on
http://127.0.0.1:8051 (started by test_cex_dash.py).

Each marker is placed on a different past minute so they are spread across
the x-axis.  The current price is fetched automatically from GET /last_price
so no manual BASE_PRICE adjustment is needed.

Usage:
    1. Start the live chart:  python test_cex_dash.py
    2. In a second terminal:  python test_plot_markers.py
"""

import logging
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests

from dash_plot import MARKER_STYLES

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_URL  = 'http://127.0.0.1:8051'
TRADE_URL = f'{BASE_URL}/trade'
PRICE_URL = f'{BASE_URL}/last_price'


# Fetch the current close price from the live chart so markers are placed
# at the correct level without any manual BASE_PRICE adjustment.
def _get_live_price() -> float:
    resp = requests.get(PRICE_URL, timeout=5)
    resp.raise_for_status()
    return float(resp.json()['price'])


# POST a single trade marker to the target Dash chart and log the result.
def _post_marker(action: str, price: float, date: datetime) -> None:
    resp = requests.post(
        TRADE_URL,
        json={'action': action, 'price': price, 'date': date.isoformat()},
        timeout=5,
    )
    label = MARKER_STYLES[action]['label']
    logger.info('  %-35s price=%.2f  → HTTP %s', label, price, resp.status_code)


def main() -> None:
    current_price = _get_live_price()
    logger.info('Live price from chart: %.2f', current_price)

    now = datetime.now(timezone.utc)

    # each marker gets a different past minute so they spread across the x-axis;
    # a small price offset prevents vertical overlap when two markers share a minute
    marker_placements = [
        # (action,               minutes_ago, price_offset)
        ('enter_long',            11,  -30),
        ('enter_short',           10,  +30),
        ('exit_long_sl',           9,  -20),
        ('exit_long_tp',           8,  +20),
        ('exit_long_tsl',          7,  -15),
        ('exit_long_special',      6,  +15),
        ('exit_short_sl',          5,  -20),
        ('exit_short_tp',          4,  +20),
        ('exit_short_tsl',         3,  -15),
        ('exit_short_special',     2,  +15),
        ('reverse_short_long',     1,  -10),
        ('reverse_long_short',     0,  +10),
    ]

    logger.info('Posting %d markers to %s', len(marker_placements), TRADE_URL)
    for action, minutes_ago, price_offset in marker_placements:
        _post_marker(action, current_price + price_offset, now - timedelta(minutes=minutes_ago))

    logger.info('Done. Markers should appear on the chart within 1 second.')


if __name__ == '__main__':
    main()
