"""Integration tests for matching and market data publication."""

from __future__ import annotations

from decimal import Decimal

from common.config import Settings, Topics
from common.models import MarketDataKind, Order, OrderStatus, Side
from common.solace_client import InMemoryBroker, MessageEnvelope
from exchange.matching_engine import MatchingEngine
from market_data.feed import MarketDataFeed


class DeterministicClock:
    def __init__(self) -> None:
        self.value = 2_000

    def __call__(self) -> int:
        self.value += 1
        return self.value


def make_order(order_id: str, side: Side, quantity: int, price: str, trader_id: str) -> Order:
    return Order(
        order_id=order_id,
        symbol="FOO",
        side=side,
        quantity=quantity,
        price=Decimal(price),
        trader_id=trader_id,
        timestamp_ns=1,
    )


def test_matching_engine_publishes_acks_fills_and_market_data() -> None:
    settings = Settings(topic_prefix="test-lab", in_memory_bus=True)
    topics = Topics(settings.topic_prefix)
    bus = InMemoryBroker()
    engine = MatchingEngine(bus, settings=settings, clock=DeterministicClock())
    feed = MarketDataFeed(bus, settings=settings)
    observed_md: list[MessageEnvelope] = []
    observed_fills: list[MessageEnvelope] = []

    bus.connect()
    feed.start()
    bus.subscribe(topics.md_l1("FOO"), observed_md.append)
    bus.subscribe(topics.md_trades("FOO"), observed_md.append)
    bus.subscribe(topics.venue_fills("FOO"), observed_fills.append)

    engine.process_order(make_order("sell", Side.SELL, 10, "100.00", "seller"))
    published = engine.process_order(make_order("buy", Side.BUY, 4, "100.00", "buyer"))

    assert [ack.status for ack in published.acks][-1] is OrderStatus.FILLED
    assert published.fills_count == 1
    assert observed_fills[0].payload["price"] == "100.00"
    assert any(event.payload["kind"] == MarketDataKind.TRADE for event in observed_md)
    assert any(event.payload["kind"] == MarketDataKind.L1 for event in observed_md)


def test_engine_subscribes_to_order_topic_payloads() -> None:
    settings = Settings(topic_prefix="test-lab", in_memory_bus=True)
    topics = Topics(settings.topic_prefix)
    bus = InMemoryBroker()
    engine = MatchingEngine(bus, settings=settings, clock=DeterministicClock())
    acks: list[MessageEnvelope] = []

    engine.start()
    bus.subscribe(topics.venue_acks("FOO"), acks.append)
    bus.publish(
        topics.venue_orders("FOO"),
        make_order("bid", Side.BUY, 10, "99.50", "buyer").to_payload(),
    )

    assert acks[-1].payload["status"] == OrderStatus.RESTING
    assert engine.snapshot("FOO").bids[0].price == Decimal("99.50")
