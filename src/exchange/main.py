"""Exchange service entry point."""

from __future__ import annotations

import time
from typing import Any

import uvicorn
from fastapi import FastAPI

from common.config import get_settings
from common.models import AmendRequest, CancelRequest, Order
from exchange.matching_engine import MatchingEngine


settings = get_settings()
engine = MatchingEngine(settings=settings)
app = FastAPI(title="Solace Trading Lab Exchange", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    """Subscribe to Solace when the HTTP service starts."""

    engine.start()


@app.get("/health")
def health() -> dict[str, Any]:
    """Expose a simple readiness endpoint for scripts and demos."""

    return {"status": "ok", "time_ns": time.time_ns()}


@app.get("/book/{symbol}")
def book(symbol: str) -> dict[str, Any]:
    """Return current depth for inspection outside Solace."""

    return engine.snapshot(symbol, depth=settings.market_data_depth).to_payload()


@app.post("/orders")
def submit_order(order: Order) -> dict[str, Any]:
    """Submit an order through HTTP for quick local experiments."""

    return engine.process_order(order).snapshot.to_payload()


@app.post("/orders/cancel")
def cancel_order(request: CancelRequest) -> dict[str, Any]:
    """Cancel through HTTP while preserving the same exchange code path."""

    return engine.process_cancel(request).snapshot.to_payload()


@app.post("/orders/amend")
def amend_order(request: AmendRequest) -> dict[str, Any]:
    """Amend through HTTP while preserving the same exchange code path."""

    return engine.process_amend(request).snapshot.to_payload()


def run() -> None:
    """Run the exchange HTTP + Solace service."""

    uvicorn.run(app, host="0.0.0.0", port=settings.exchange_http_port)


if __name__ == "__main__":
    run()
