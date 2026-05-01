"""Venue orchestration around one or more order books."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from common.config import Settings, Topics, get_settings
from common.models import AmendRequest, BookSnapshot, CancelRequest, Order, OrderAck, OrderStatus
from common.solace_client import MessageBus, MessageEnvelope, SolaceClient
from exchange.orderbook import Clock, OrderBook, OrderBookResult


@dataclass(slots=True)
class PublishedVenueEvents:
    """Typed view of what the engine published for one command."""

    acks: list[OrderAck]
    fills_count: int
    snapshot: BookSnapshot


class MatchingEngine:
    """Coordinate books and publish venue events."""

    def __init__(
        self,
        bus: MessageBus | None = None,
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
        book_factory: Callable[[str], OrderBook] | None = None,
    ) -> None:
        self.settings = (settings or get_settings()).normalized()
        self.topics = Topics(self.settings.topic_prefix)
        self.bus = bus or SolaceClient(self.settings)
        self._clock = clock or time.time_ns
        self._book_factory = book_factory or (lambda symbol: OrderBook(symbol, clock=self._clock))
        self._books: dict[str, OrderBook] = {}

    def start(self) -> None:
        """Connect and subscribe to order-command topics."""

        self.bus.connect()
        self.bus.subscribe(self.topics.venue_order_wildcard(), self.handle_order_envelope)

    def book(self, symbol: str) -> OrderBook:
        """Return the book for a symbol, creating it lazily on first order."""

        normalized = symbol.upper()
        if normalized not in self._books:
            self._books[normalized] = self._book_factory(normalized)
        return self._books[normalized]

    def process_order(self, order: Order) -> PublishedVenueEvents:
        """Submit an order and publish resulting venue events."""

        result = self.book(order.symbol).submit(order)
        return self._publish_result(order.symbol, result)

    def process_cancel(self, request: CancelRequest) -> PublishedVenueEvents:
        """Cancel a resting order and publish the resulting state."""

        result = self.book(request.symbol).cancel(request)
        return self._publish_result(request.symbol, result)

    def process_amend(self, request: AmendRequest) -> PublishedVenueEvents:
        """Amend a resting order and publish the resulting state."""

        result = self.book(request.symbol).amend(request)
        return self._publish_result(request.symbol, result)

    def handle_order_envelope(self, envelope: MessageEnvelope) -> None:
        """Dispatch a broker order command payload."""

        payload = dict(envelope.payload)
        command = str(payload.pop("command", "NEW")).upper()
        if command == "NEW":
            self.process_order(Order.model_validate(payload))
        elif command == "CANCEL":
            self.process_cancel(CancelRequest.model_validate(payload))
        elif command == "AMEND":
            self.process_amend(AmendRequest.model_validate(payload))
        else:
            symbol = str(payload.get("symbol", self.settings.symbol)).upper()
            ack = OrderAck(
                order_id=str(payload.get("order_id", "UNKNOWN")),
                symbol=symbol,
                status=OrderStatus.REJECTED,
                sequence=0,
                timestamp_ns=self._clock(),
                reason=f"unknown command {command}",
            )
            self.bus.publish(self.topics.venue_acks(symbol), ack)

    def snapshot(self, symbol: str, *, depth: int | None = None) -> BookSnapshot:
        """Read current book state without publishing."""

        return self.book(symbol).snapshot(depth=depth)

    def _publish_result(self, symbol: str, result: OrderBookResult) -> PublishedVenueEvents:
        for ack in result.acks:
            self.bus.publish(self.topics.venue_acks(symbol), ack)
        for fill in result.fills:
            self.bus.publish(self.topics.venue_fills(symbol), fill)
        self.bus.publish(self.topics.venue_book(symbol), result.snapshot)
        return PublishedVenueEvents(
            acks=result.acks,
            fills_count=len(result.fills),
            snapshot=result.snapshot,
        )
