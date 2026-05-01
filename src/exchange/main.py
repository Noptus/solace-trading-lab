"""Venue service entry point."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI

from common.config import get_settings
from common.models import AmendRequest, CancelRequest, Order
from exchange.matching_engine import MatchingEngine

settings = get_settings().with_client_name("venue")
engine = MatchingEngine(settings=settings)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    engine.start()
    yield
    engine.bus.close()


app = FastAPI(title="Solace Trading Lab Venue", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    """Return process readiness."""

    return {"status": "ok", "time_ns": time.time_ns()}


@app.get("/book/{symbol}")
def book(symbol: str) -> dict[str, Any]:
    """Return current depth outside Solace."""

    return engine.snapshot(symbol, depth=settings.market_data_depth).to_payload()


@app.post("/orders")
def submit_order(order: Order) -> dict[str, Any]:
    """Submit an order through HTTP."""

    return engine.process_order(order).snapshot.to_payload()


@app.post("/orders/cancel")
def cancel_order(request: CancelRequest) -> dict[str, Any]:
    """Cancel through the same venue code path."""

    return engine.process_cancel(request).snapshot.to_payload()


@app.post("/orders/amend")
def amend_order(request: AmendRequest) -> dict[str, Any]:
    """Amend through the same venue code path."""

    return engine.process_amend(request).snapshot.to_payload()


def run() -> None:
    """Run the venue HTTP and Solace service."""

    uvicorn.run(app, host="0.0.0.0", port=settings.venue_http_port)


if __name__ == "__main__":
    run()
