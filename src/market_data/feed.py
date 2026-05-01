"""Market data projection service.

The exchange publishes facts: fills and book snapshots.  This module derives
the lower-bandwidth views strategies normally consume: L1, L2, and trade prints.
That separation keeps market-data latency and exchange matching independent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from common.config import Settings, Topics, get_settings
from common.models import BookSnapshot, Fill, MarketData, MarketDataKind
from common.solace_client import MessageBus, MessageEnvelope, SolaceClient

LatencyHook = Callable[[str, dict[str, object]], None]


@dataclass(slots=True)
class MarketDataPublication:
    """One market data event emitted by the feed."""

    topic: str
    data: MarketData


class MarketDataFeed:
    """Subscribe to exchange events and publish market data topics."""

    def __init__(
        self,
        bus: MessageBus | None = None,
        *,
        settings: Settings | None = None,
        latency_hook: LatencyHook | None = None,
    ) -> None:
        self.settings = (settings or get_settings()).normalized()
        self.topics = Topics(self.settings.topic_prefix)
        self.bus = bus or SolaceClient(self.settings)
        self.latency_hook = latency_hook

    def start(self) -> None:
        """Connect and subscribe to exchange book/fill streams."""

        self.bus.connect()
        self.bus.subscribe(self.topics.exchange_book_wildcard(), self.handle_book_envelope)
        self.bus.subscribe(self.topics.exchange_fill_wildcard(), self.handle_fill_envelope)

    def handle_book_envelope(self, envelope: MessageEnvelope) -> None:
        snapshot = BookSnapshot.model_validate(envelope.payload)
        self.publish_book(snapshot)

    def handle_fill_envelope(self, envelope: MessageEnvelope) -> None:
        fill = Fill.model_validate(envelope.payload)
        self.publish_trade(fill)

    def publish_book(self, snapshot: BookSnapshot) -> list[MarketDataPublication]:
        """Publish L1 and L2 views for a book snapshot."""

        depth = self.settings.market_data_depth
        trimmed = snapshot.model_copy(
            update={"bids": snapshot.bids[:depth], "asks": snapshot.asks[:depth]}
        )
        l1 = trimmed.l1()
        publications = [
            MarketDataPublication(
                topic=self.topics.md_l1(snapshot.symbol),
                data=MarketData(
                    kind=MarketDataKind.L1,
                    symbol=snapshot.symbol,
                    sequence=snapshot.sequence,
                    timestamp_ns=snapshot.timestamp_ns,
                    payload=l1.to_payload(),
                ),
            ),
            MarketDataPublication(
                topic=self.topics.md_l2(snapshot.symbol),
                data=MarketData(
                    kind=MarketDataKind.L2,
                    symbol=snapshot.symbol,
                    sequence=snapshot.sequence,
                    timestamp_ns=snapshot.timestamp_ns,
                    payload=trimmed.to_payload(),
                ),
            ),
        ]
        for publication in publications:
            self._publish(publication)
        return publications

    def publish_trade(self, fill: Fill) -> MarketDataPublication:
        """Publish a trade print from an exchange fill."""

        publication = MarketDataPublication(
            topic=self.topics.md_trades(fill.symbol),
            data=MarketData(
                kind=MarketDataKind.TRADE,
                symbol=fill.symbol,
                sequence=fill.sequence,
                timestamp_ns=fill.timestamp_ns,
                payload=fill.to_payload(),
            ),
        )
        self._publish(publication)
        return publication

    def _publish(self, publication: MarketDataPublication) -> None:
        payload = publication.data.to_payload()
        if self.latency_hook is not None:
            self.latency_hook(publication.topic, payload)
        self.bus.publish(publication.topic, payload)


def fixed_latency_hook(delay_ms: int) -> LatencyHook:
    """Create a simple blocking latency hook for local demonstrations."""

    def _hook(_topic: str, _payload: dict[str, object]) -> None:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

    return _hook
