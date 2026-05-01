"""Strategy base classes.

Strategies consume market-data events and produce orders, but they do not know
whether those events came from live Solace, replay, or a unit test.  This is the
boundary that makes strategy behavior deterministic and benchmarkable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from common.config import Settings, Topics, get_settings
from common.models import Fill, MarketData, Order
from common.solace_client import MessageBus, MessageEnvelope, SolaceClient


@dataclass(slots=True)
class Position:
    """Inventory and cash state for one symbol."""

    inventory: int = 0
    cash: Decimal = Decimal("0")

    def mark_to_market(self, mid_price: Decimal) -> Decimal:
        """Return cash plus inventory valued at a supplied mid price."""

        return self.cash + Decimal(self.inventory) * mid_price


class Strategy(ABC):
    """Abstract strategy wired to market data, fills, and order publication."""

    def __init__(
        self,
        strategy_id: str,
        symbol: str,
        bus: MessageBus | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.symbol = symbol.upper()
        self.settings = (settings or get_settings()).normalized()
        self.topics = Topics(self.settings.topic_prefix)
        self.bus = bus or SolaceClient(self.settings)
        self.position = Position()
        self.orders_sent: list[Order] = []
        self.fills: list[Fill] = []

    def start(self) -> None:
        """Subscribe to market data and fills for this strategy's symbol."""

        self.bus.connect()
        self.bus.subscribe(self.topics.md_l1(self.symbol), self.handle_market_data_envelope)
        self.bus.subscribe(self.topics.md_trades(self.symbol), self.handle_market_data_envelope)
        self.bus.subscribe(self.topics.exchange_fills(self.symbol), self.handle_fill_envelope)

    def handle_market_data_envelope(self, envelope: MessageEnvelope) -> None:
        data = MarketData.model_validate(envelope.payload)
        for order in self.on_market_data(data):
            self.send_order(order)

    def handle_fill_envelope(self, envelope: MessageEnvelope) -> None:
        fill = Fill.model_validate(envelope.payload)
        self.on_fill(fill)

    def send_order(self, order: Order) -> None:
        """Publish an order and retain it for deterministic accounting."""

        self.orders_sent.append(order)
        self.bus.publish(self.topics.exchange_orders(order.symbol), order)

    def on_fill(self, fill: Fill) -> None:
        """Update P&L only for fills involving this strategy."""

        touched = False
        if fill.buyer_order_id in {order.order_id for order in self.orders_sent}:
            self.position.inventory += fill.quantity
            self.position.cash -= fill.price * fill.quantity
            touched = True
        if fill.seller_order_id in {order.order_id for order in self.orders_sent}:
            self.position.inventory -= fill.quantity
            self.position.cash += fill.price * fill.quantity
            touched = True
        if touched:
            self.fills.append(fill)

    @abstractmethod
    def on_market_data(self, data: MarketData) -> list[Order]:
        """Return orders triggered by one market-data event."""
