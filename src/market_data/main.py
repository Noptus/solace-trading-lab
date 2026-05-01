"""Market data service entry point."""

from __future__ import annotations

import signal
import threading

from common.config import get_settings
from market_data.feed import MarketDataFeed


def run() -> None:
    """Run the feed as a long-lived Solace subscriber."""

    settings = get_settings().with_client_name("market-data")
    feed = MarketDataFeed(settings=settings)
    feed.start()

    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    shutdown.wait()
    feed.bus.close()


if __name__ == "__main__":
    run()
