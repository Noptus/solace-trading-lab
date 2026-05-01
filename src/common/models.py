"""Pydantic schemas for venue, market data, chaos, and replay events."""

from __future__ import annotations

import time
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OrderId: TypeAlias = str
Symbol: TypeAlias = str
Price: TypeAlias = Decimal
Qty: TypeAlias = int
Nanos: TypeAlias = int
Sequence: TypeAlias = int
Payload: TypeAlias = dict[str, Any]


def utc_now_ns() -> Nanos:
    """Return wall-clock nanoseconds for live services."""

    return time.time_ns()


class JsonModel(BaseModel):
    """Strict model with broker-safe serialization helpers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=False,
        populate_by_name=True,
        validate_assignment=True,
    )

    def to_payload(self) -> Payload:
        """Encode Decimals and enums for broker JSON."""

        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Serialize through the broker encoding path."""

        return self.model_dump_json()

    @classmethod
    def from_payload(cls, payload: Payload | str | bytes) -> Self:
        """Validate broker JSON into a typed event."""

        if isinstance(payload, str | bytes):
            return cls.model_validate_json(payload)
        return cls.model_validate(payload)


class Side(StrEnum):
    """Order side from the taker perspective."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        """Return the contra book side."""

        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    """Supported order styles."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    """Execution constraints supported by the deterministic book."""

    GTC = "GTC"
    IOC = "IOC"


class OrderStatus(StrEnum):
    """Execution report states emitted by the venue."""

    ACCEPTED = "ACCEPTED"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    AMENDED = "AMENDED"


class MarketDataKind(StrEnum):
    """Market data channels derived from venue state changes."""

    L1 = "L1"
    L2 = "L2"
    TRADE = "TRADE"


class ChaosEventKind(StrEnum):
    """Observable infrastructure effects injected by the chaos agent."""

    LATENCY = "LATENCY"
    DROP = "DROP"


class Order(JsonModel):
    """Client order command with caller-supplied id for deterministic replay."""

    order_id: OrderId = Field(min_length=1)
    symbol: Symbol = Field(min_length=1)
    side: Side
    quantity: Qty = Field(gt=0)
    price: Price | None = Field(default=None, gt=Decimal("0"))
    trader_id: str = Field(default="anonymous", min_length=1)
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.GTC
    timestamp_ns: Nanos = Field(default_factory=utc_now_ns, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        """Prevent symbol case from partitioning books and topics."""

        return value.upper()

    @model_validator(mode="after")
    def validate_price_semantics(self) -> Order:
        """Reject ambiguous order price semantics."""

        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("limit orders require a price")
        if self.order_type is OrderType.MARKET and self.time_in_force is TimeInForce.GTC:
            self.time_in_force = TimeInForce.IOC
        return self


class CancelRequest(JsonModel):
    """Cancel an outstanding resting order by id."""

    order_id: OrderId = Field(min_length=1)
    symbol: Symbol = Field(min_length=1)
    trader_id: str = Field(default="anonymous", min_length=1)
    timestamp_ns: Nanos = Field(default_factory=utc_now_ns, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class AmendRequest(JsonModel):
    """Cancel-replace request that resets venue queue priority."""

    order_id: OrderId = Field(min_length=1)
    symbol: Symbol = Field(min_length=1)
    trader_id: str = Field(default="anonymous", min_length=1)
    new_quantity: Qty | None = Field(default=None, gt=0)
    new_price: Price | None = Field(default=None, gt=Decimal("0"))
    timestamp_ns: Nanos = Field(default_factory=utc_now_ns, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class Fill(JsonModel):
    """Trade caused by a taker consuming a resting maker order."""

    fill_id: str = Field(min_length=1)
    symbol: Symbol = Field(min_length=1)
    price: Price = Field(gt=Decimal("0"))
    quantity: Qty = Field(gt=0)
    maker_order_id: OrderId = Field(min_length=1)
    taker_order_id: OrderId = Field(min_length=1)
    buyer_order_id: OrderId = Field(min_length=1)
    seller_order_id: OrderId = Field(min_length=1)
    maker_trader_id: str = Field(min_length=1)
    taker_trader_id: str = Field(min_length=1)
    sequence: Sequence = Field(ge=1)
    timestamp_ns: Nanos = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class OrderAck(JsonModel):
    """Execution report emitted after an order command changes venue state."""

    order_id: OrderId = Field(min_length=1)
    symbol: Symbol = Field(min_length=1)
    status: OrderStatus
    accepted_quantity: Qty = Field(default=0, ge=0)
    remaining_quantity: Qty = Field(default=0, ge=0)
    filled_quantity: Qty = Field(default=0, ge=0)
    queue_position: Qty | None = Field(default=None, ge=0)
    sequence: Sequence = Field(ge=0)
    timestamp_ns: Nanos = Field(ge=0)
    reason: str | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class BookLevel(JsonModel):
    """Aggregated depth at one price level."""

    price: Price = Field(gt=Decimal("0"))
    quantity: Qty = Field(ge=0)
    order_count: int = Field(ge=0)


class L1Quote(JsonModel):
    """Best bid and offer derived from aggregated depth."""

    symbol: Symbol = Field(min_length=1)
    bid_price: Price | None = None
    bid_quantity: Qty = Field(default=0, ge=0)
    ask_price: Price | None = None
    ask_quantity: Qty = Field(default=0, ge=0)
    sequence: Sequence = Field(ge=0)
    timestamp_ns: Nanos = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()


class BookSnapshot(JsonModel):
    """Point-in-time aggregated order book depth."""

    symbol: Symbol = Field(min_length=1)
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    sequence: Sequence = Field(ge=0)
    timestamp_ns: Nanos = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()

    def l1(self) -> L1Quote:
        """Derive top of book from aggregated depth."""

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
    """Typed market data envelope with a replay-safe payload."""

    kind: MarketDataKind
    symbol: Symbol = Field(min_length=1)
    sequence: Sequence = Field(ge=0)
    timestamp_ns: Nanos = Field(ge=0)
    payload: Payload

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
    sequence: Sequence = Field(ge=1)
    timestamp_ns: Nanos = Field(default_factory=utc_now_ns, ge=0)


class RecordedMessage(JsonModel):
    """Replayable broker message persisted as one JSON line."""

    sequence: Sequence = Field(ge=1)
    topic: str = Field(min_length=1)
    payload: Payload
    timestamp_ns: Nanos = Field(ge=0)
