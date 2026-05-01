"""Market data service entry point."""

from __future__ import annotations

import time

from common.config import get_settings
from market_data.feed import MarketDataFeed


def run() -> None:
    """Run the feed as a long-lived Solace subscriber."""

    feed = MarketDataFeed(settings=get_settings())
    feed.start()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    run()
