from __future__ import annotations

from decimal import Decimal

from common.config import Settings, Topics
from common.models import Fill, MarketData, MarketDataKind, Side
from common.solace_client import InMemoryBroker, MessageEnvelope
from strategy.market_maker import MarketMaker


def l1_payload() -> MarketData:
    return MarketData(
        kind=MarketDataKind.L1,
        symbol="FOO",
        sequence=1,
        timestamp_ns=10,
        payload={
            "symbol": "FOO",
            "bid_price": "100.00",
            "bid_quantity": 10,
            "ask_price": "100.20",
            "ask_quantity": 10,
            "sequence": 1,
            "timestamp_ns": 10,
        },
    )


def test_market_maker_generates_deterministic_two_sided_quotes() -> None:
    strategy = MarketMaker(
        "mm",
        "FOO",
        quote_size=3,
        spread=Decimal("0.10"),
        tick_size=Decimal("0.01"),
        settings=Settings(topic_prefix="test-lab", in_memory_bus=True),
        bus=InMemoryBroker(),
    )

    orders = strategy.on_market_data(l1_payload())

    assert [order.side for order in orders] == [Side.BUY, Side.SELL]
    assert [order.order_id for order in orders] == ["mm-FOO-000001", "mm-FOO-000002"]
    assert orders[0].price == Decimal("100.00")
    assert orders[1].price == Decimal("100.20")
    assert all(order.quantity == 3 for order in orders)


def test_strategy_start_publishes_orders_from_market_data() -> None:
    settings = Settings(topic_prefix="test-lab", in_memory_bus=True)
    topics = Topics(settings.topic_prefix)
    bus = InMemoryBroker()
    strategy = MarketMaker("mm", "FOO", settings=settings, bus=bus)
    observed_orders: list[MessageEnvelope] = []

    strategy.start()
    bus.subscribe(topics.exchange_orders("FOO"), observed_orders.append)
    bus.publish(topics.md_l1("FOO"), l1_payload())

    assert len(observed_orders) == 2
    assert observed_orders[0].payload["order_id"] == "mm-FOO-000001"


def test_strategy_accounts_for_own_fills_only() -> None:
    settings = Settings(topic_prefix="test-lab", in_memory_bus=True)
    strategy = MarketMaker("mm", "FOO", settings=settings, bus=InMemoryBroker())
    own_buy = strategy.on_market_data(l1_payload())[0]
    strategy.orders_sent.append(own_buy)
    fill = Fill(
        fill_id="FOO-000000000001",
        symbol="FOO",
        price=Decimal("100.00"),
        quantity=2,
        maker_order_id="seller",
        taker_order_id=own_buy.order_id,
        buyer_order_id=own_buy.order_id,
        seller_order_id="seller",
        maker_trader_id="seller",
        taker_trader_id="mm",
        sequence=1,
        timestamp_ns=10,
    )

    strategy.on_fill(fill)

    assert strategy.position.inventory == 2
    assert strategy.position.cash == Decimal("-200.00")
