"""Strategy service entry point."""

from __future__ import annotations

import time
from decimal import Decimal

from common.config import get_settings
from strategy.market_maker import MarketMaker


def run() -> None:
    """Run the example market maker as a long-lived service."""

    settings = get_settings()
    strategy = MarketMaker(
        "mm-001",
        settings.symbol,
        settings=settings,
        quote_size=10,
        spread=Decimal("0.10"),
    )
    strategy.start()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    run()
