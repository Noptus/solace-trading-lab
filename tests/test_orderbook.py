"""Unit tests for deterministic order book behavior."""

from __future__ import annotations

from decimal import Decimal

import pytest

from common.models import AmendRequest, CancelRequest, Order, OrderStatus, Side
from exchange.orderbook import BookInvariantViolation, OrderBook


class DeterministicClock:
    def __init__(self) -> None:
        self.value = 1_000

    def __call__(self) -> int:
        self.value += 1
        return self.value


def order(
    order_id: str,
    side: Side,
    quantity: int,
    price: str,
    *,
    trader_id: str = "trader",
) -> Order:
    return Order(
        order_id=order_id,
        symbol="FOO",
        side=side,
        quantity=quantity,
        price=Decimal(price),
        trader_id=trader_id,
        timestamp_ns=1,
    )


def test_price_time_priority_at_same_level() -> None:
    book = OrderBook("FOO", clock=DeterministicClock())
    book.submit(order("ask-1", Side.SELL, 5, "100.50", trader_id="seller-a"))
    book.submit(order("ask-2", Side.SELL, 7, "100.50", trader_id="seller-b"))

    assert book.queue_position("ask-1") == 0
    assert book.queue_position("ask-2") == 5

    result = book.submit(order("buy-1", Side.BUY, 6, "100.50", trader_id="buyer"))

    assert [fill.maker_order_id for fill in result.fills] == ["ask-1", "ask-2"]
    assert [fill.quantity for fill in result.fills] == [5, 1]
    assert all(fill.price == Decimal("100.50") for fill in result.fills)
    assert book.queue_position("ask-2") == 0
    assert result.snapshot.asks[0].quantity == 6


def test_better_price_beats_earlier_time() -> None:
    book = OrderBook("FOO", clock=DeterministicClock())
    book.submit(order("ask-expensive", Side.SELL, 5, "101.00"))
    book.submit(order("ask-cheap", Side.SELL, 5, "100.00"))

    result = book.submit(order("buy", Side.BUY, 5, "101.00"))

    assert len(result.fills) == 1
    assert result.fills[0].maker_order_id == "ask-cheap"
    assert result.fills[0].price == Decimal("100.00")
    assert result.snapshot.asks[0].price == Decimal("101.00")


def test_cancel_removes_resting_order_and_preserves_others() -> None:
    book = OrderBook("FOO", clock=DeterministicClock())
    book.submit(order("bid-1", Side.BUY, 5, "99.50", trader_id="alice"))
    book.submit(order("bid-2", Side.BUY, 5, "99.50", trader_id="bob"))

    result = book.cancel(
        CancelRequest(order_id="bid-1", symbol="FOO", trader_id="alice", timestamp_ns=10)
    )

    assert result.acks[0].status is OrderStatus.CANCELED
    assert book.queue_position("bid-1") is None
    assert book.queue_position("bid-2") == 0
    assert result.snapshot.bids[0].quantity == 5


def test_order_ids_cannot_be_reused_after_fill() -> None:
    book = OrderBook("FOO", clock=DeterministicClock())
    book.submit(order("ask", Side.SELL, 1, "100.00"))
    book.submit(order("buy", Side.BUY, 1, "100.00"))

    duplicate = book.submit(order("buy", Side.BUY, 1, "99.00"))

    assert duplicate.acks[0].status is OrderStatus.REJECTED
    assert duplicate.acks[0].reason == "duplicate order id"


def test_invariant_report_summarizes_resting_liquidity_after_lifecycle() -> None:
    book = OrderBook("FOO", clock=DeterministicClock())
    book.submit(order("bid-1", Side.BUY, 5, "99.50", trader_id="alice"))
    book.submit(order("ask-1", Side.SELL, 7, "100.50", trader_id="bob"))
    book.amend(
        AmendRequest(
            order_id="bid-1",
            symbol="FOO",
            trader_id="alice",
            new_quantity=3,
            timestamp_ns=20,
        )
    )
    book.cancel(CancelRequest(order_id="ask-1", symbol="FOO", trader_id="bob", timestamp_ns=30))

    report = book.validate_invariants()

    assert report.symbol == "FOO"
    assert report.active_orders == 1
    assert report.bid_levels == 1
    assert report.ask_levels == 0
    assert report.total_bid_quantity == 3
    assert report.total_ask_quantity == 0


def test_invariant_validation_detects_stale_order_index() -> None:
    book = OrderBook("FOO", clock=DeterministicClock())
    book.submit(order("bid-1", Side.BUY, 5, "99.50"))
    book._bids.clear()

    with pytest.raises(BookInvariantViolation, match="order index mismatch"):
        book.validate_invariants()
