"""Strategy service entry point."""

from __future__ import annotations

import signal
import threading
from decimal import Decimal
from typing import Any

import uvicorn
from fastapi import FastAPI

from common.config import get_settings
from strategy.market_maker import MarketMaker

settings = get_settings().with_client_name("strategy")
strategy = MarketMaker(
    "mm-001",
    settings.symbol,
    settings=settings,
    quote_size=10,
    spread=Decimal("0.10"),
)

app = FastAPI(title="Solace Trading Lab Strategy", version="0.1.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "strategy_id": strategy.strategy_id}


@app.get("/position")
def position() -> dict[str, Any]:
    mid = None
    if strategy.fills:
        mid = strategy.fills[-1].price
    mtm = strategy.position.mark_to_market(mid) if mid else strategy.position.cash
    return {
        "strategy_id": strategy.strategy_id,
        "symbol": strategy.symbol,
        "inventory": strategy.position.inventory,
        "cash": str(strategy.position.cash),
        "mark_to_market": str(mtm),
        "orders_sent": len(strategy.orders_sent),
        "fills": len(strategy.fills),
    }


def run() -> None:
    """Run the example market maker with an HTTP status endpoint."""

    strategy.start()

    shutdown = threading.Event()

    def _shutdown(*_: Any) -> None:
        shutdown.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    port = settings.venue_http_port + 1
    server_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning"),
        daemon=True,
    )
    server_thread.start()
    shutdown.wait()
    strategy.bus.close()


if __name__ == "__main__":
    run()
