"""Canonical event schemas used by every service.

The lab is intentionally event-sourced: services should exchange durable facts
instead of Python objects with hidden process state.  These Pydantic models make
that contract explicit and keep replay deterministic because every downstream
consumer sees the same validated payload.
"""

from __future__ import annotations

import time
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now_ns() -> int:
    """Return wall-clock nanoseconds for live services.

    Tests and replay pass explicit timestamps or injected clocks.  Keeping this
    helper in one place makes the non-deterministic edge obvious.
    """

    return time.time_ns()


class JsonModel(BaseModel):
    """Base model with strict validation and JSON-safe dumping helpers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=False,
        populate_by_name=True,
        validate_assignment=True,
    )

    def to_payload(self) -> dict[str, Any]:
        """Return a broker-safe payload with Decimals and enums encoded."""

        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Serialize using the same encoding path used for broker publishing."""

        return self.model_dump_json()

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | str | bytes) -> Self:
        """Validate a JSON string/bytes or decoded mapping into a typed event."""

        if isinstance(payload, str | bytes):
            return cls.model_validate_json(payload)
        return cls.model_validate(payload)


class Side(StrEnum):
    """Order side, named from the taker's perspective."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        """Return the contra side used when routing into the opposite book."""

        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    """Supported order styles for the MVP exchange."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    """Execution constraints supported by the deterministic book."""

    GTC = "GTC"
    IOC = "IOC"


class OrderStatus(StrEnum):
    """Execution report states emitted by the exchange."""

    ACCEPTED = "ACCEPTED"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    AMENDED = "AMENDED"


class MarketDataKind(StrEnum):
    """Market data channels derived from exchange state changes."""

    L1 = "L1"
    L2 = "L2"
    TRADE = "TRADE"


class ChaosEventKind(StrEnum):
    """Observable infrastructure effects injected by the chaos agent."""

    LATENCY = "LATENCY"
    DROP = "DROP"


class Order(JsonModel):
    """A client order submitted to the exchange.

    The order id is required instead of generated here because deterministic
    labs and replay should be able to recreate the exact same event stream.
    """

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    quantity: int = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=Decimal("0"))
    trader_id: str = Field(default="anonymous", min_length=1)
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.GTC
    timestamp_ns: int = Field(default_factory=utc_now_ns, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        """Normalize symbols so topic names and books do not diverge by case."""

        return value.upper()

    @model_validator(mode="after")
    def validate_price_semantics(self) -> "Order":
        """Reject ambiguous prices at the schema boundary."""

        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("limit orders require a price")
        if self.order_type is OrderType.MARKET and self.time_in_force is TimeInForce.GTC:
            self.time_in_force = TimeInForce.IOC
        return self


class CancelRequest(JsonModel):
    """Cancel an outstanding resting order by id."""

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    trader_id: str = Field(default="anonymous", min_length=1)
    timestamp_ns: int = Field(default_factory=utc_now_ns, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class AmendRequest(JsonModel):
    """Replace an outstanding order while preserving the external order id.

    Amends deliberately lose queue priority in the exchange implementation.
    That mirrors common market behavior and keeps the educational model simple.
    """

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    trader_id: str = Field(default="anonymous", min_length=1)
    new_quantity: int | None = Field(default=None, gt=0)
    new_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    timestamp_ns: int = Field(default_factory=utc_now_ns, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class Fill(JsonModel):
    """A trade caused by a taker order consuming a resting maker order."""

    fill_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    price: Decimal = Field(gt=Decimal("0"))
    quantity: int = Field(gt=0)
    maker_order_id: str = Field(min_length=1)
    taker_order_id: str = Field(min_length=1)
    buyer_order_id: str = Field(min_length=1)
    seller_order_id: str = Field(min_length=1)
    maker_trader_id: str = Field(min_length=1)
    taker_trader_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp_ns: int = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class OrderAck(JsonModel):
    """Execution report emitted after an order event changes exchange state."""

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    status: OrderStatus
    accepted_quantity: int = Field(default=0, ge=0)
    remaining_quantity: int = Field(default=0, ge=0)
    filled_quantity: int = Field(default=0, ge=0)
    queue_position: int | None = Field(default=None, ge=0)
    sequence: int = Field(ge=0)
    timestamp_ns: int = Field(ge=0)
    reason: str | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class BookLevel(JsonModel):
    """Aggregated depth at one price level."""

    price: Decimal = Field(gt=Decimal("0"))
    quantity: int = Field(ge=0)
    order_count: int = Field(ge=0)


class L1Quote(JsonModel):
    """Best bid and offer snapshot.

    L1 is duplicated from L2 rather than inferred by consumers so replay and
    strategy tests can subscribe to the minimal event they need.
    """

    symbol: str = Field(min_length=1)
    bid_price: Decimal | None = None
    bid_quantity: int = Field(default=0, ge=0)
    ask_price: Decimal | None = None
    ask_quantity: int = Field(default=0, ge=0)
    sequence: int = Field(ge=0)
    timestamp_ns: int = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class BookSnapshot(JsonModel):
    """Point-in-time aggregated order book depth."""

    symbol: str = Field(min_length=1)
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    sequence: int = Field(ge=0)
    timestamp_ns: int = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()

    def l1(self) -> L1Quote:
        """Derive best bid/offer from the already aggregated depth."""

        best_bid = self.bids[0] if self.bids else None
        best_ask = self.asks[0] if self.asks else None
        return L1Quote(
            symbol=self.symbol,
            bid_price=best_bid.price if best_bid else None,
            bid_quantity=best_bid.quantity if best_bid else 0,
            ask_price=best_ask.price if best_ask else None,
            ask_quantity=best_ask.quantity if best_ask else 0,
            sequence=self.sequence,
            timestamp_ns=self.timestamp_ns,
        )


class MarketData(JsonModel):
    """Envelope for L1, L2, and trade-print market data.

    A small envelope lets subscribers route by kind while still preserving the
    raw typed payload for replay and later schema evolution.
    """

    kind: MarketDataKind
    symbol: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    timestamp_ns: int = Field(ge=0)
    payload: dict[str, Any]

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class ChaosEvent(JsonModel):
    """Audit event explaining an injected infrastructure effect."""

    kind: ChaosEventKind
    topic: str = Field(min_length=1)
    delay_ms: int = Field(default=0, ge=0)
    dropped: bool = False
    sequence: int = Field(ge=1)
    timestamp_ns: int = Field(default_factory=utc_now_ns, ge=0)


class RecordedMessage(JsonModel):
    """A replayable broker message persisted as one JSON line."""

    sequence: int = Field(ge=1)
    topic: str = Field(min_length=1)
    payload: dict[str, Any]
    timestamp_ns: int = Field(ge=0)
