"""Deterministic price-time priority limit order book."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from common.models import (
    AmendRequest,
    BookLevel,
    BookSnapshot,
    CancelRequest,
    Fill,
    Nanos,
    Order,
    OrderAck,
    OrderId,
    OrderStatus,
    OrderType,
    Price,
    Qty,
    Side,
    TimeInForce,
)

Clock: TypeAlias = Callable[[], Nanos]


@dataclass(slots=True)
class RestingOrder:
    """Internal order state held in a price level FIFO queue."""

    order: Order
    remaining_quantity: Qty
    arrival_sequence: int


BookSide: TypeAlias = dict[Price, deque[RestingOrder]]


@dataclass(slots=True)
class OrderBookResult:
    """Venue events produced by processing one command."""

    acks: list[OrderAck]
    fills: list[Fill]
    snapshot: BookSnapshot


class BookInvariantViolation(AssertionError):
    """Raised when internal order book indexes disagree with queued liquidity."""


@dataclass(frozen=True, slots=True)
class BookInvariantReport:
    """Compact audit summary for correctness checks and teaching output."""

    symbol: str
    active_orders: int
    bid_levels: int
    ask_levels: int
    total_bid_quantity: Qty
    total_ask_quantity: Qty


class OrderBook:
    """Price-time priority limit order book for one symbol."""

    def __init__(self, symbol: str, *, clock: Clock | None = None) -> None:
        self.symbol = symbol.upper()
        self._clock = clock or time.time_ns
        self._bids: BookSide = {}
        self._asks: BookSide = {}
        self._orders: dict[str, RestingOrder] = {}
        self._seen_order_ids: set[str] = set()
        self._event_sequence = 0
        self._trade_sequence = 0

    @property
    def sequence(self) -> int:
        """Last venue state sequence emitted by this book."""

        return self._event_sequence

    def submit(self, order: Order) -> OrderBookResult:
        """Accept, match, and optionally rest a new order."""

        self._event_sequence += 1
        timestamp_ns = self._clock()
        if order.symbol != self.symbol:
            return self._reject(
                order.order_id,
                order.symbol,
                f"order symbol {order.symbol} does not match book {self.symbol}",
                timestamp_ns,
            )
        if order.order_id in self._seen_order_ids:
            return self._reject(order.order_id, order.symbol, "duplicate order id", timestamp_ns)
        self._seen_order_ids.add(order.order_id)

        acks = [
            OrderAck(
                order_id=order.order_id,
                symbol=self.symbol,
                status=OrderStatus.ACCEPTED,
                accepted_quantity=order.quantity,
                remaining_quantity=order.quantity,
                sequence=self._event_sequence,
                timestamp_ns=timestamp_ns,
            )
        ]
        fills: list[Fill] = []
        remaining = order.quantity

        while remaining > 0 and self._can_match(order.side, order.price, order.order_type):
            level_price = self.best_ask_price if order.side is Side.BUY else self.best_bid_price
            if level_price is None:
                break
            opposite_book = self._asks if order.side is Side.BUY else self._bids
            level = opposite_book[level_price]
            while level and remaining > 0:
                maker = level[0]
                trade_quantity = min(remaining, maker.remaining_quantity)
                remaining -= trade_quantity
                maker.remaining_quantity -= trade_quantity
                fill = self._make_fill(order, maker.order, level_price, trade_quantity)
                fills.append(fill)
                maker_status = (
                    OrderStatus.FILLED
                    if maker.remaining_quantity == 0
                    else OrderStatus.PARTIALLY_FILLED
                )
                acks.append(
                    OrderAck(
                        order_id=maker.order.order_id,
                        symbol=self.symbol,
                        status=maker_status,
                        accepted_quantity=maker.order.quantity,
                        remaining_quantity=maker.remaining_quantity,
                        filled_quantity=maker.order.quantity - maker.remaining_quantity,
                        queue_position=0 if maker.remaining_quantity > 0 else None,
                        sequence=self._event_sequence,
                        timestamp_ns=timestamp_ns,
                    )
                )
                if maker.remaining_quantity == 0:
                    level.popleft()
                    self._orders.pop(maker.order.order_id, None)
            if not level:
                del opposite_book[level_price]

        filled_quantity = order.quantity - remaining
        if remaining == 0:
            acks.append(
                OrderAck(
                    order_id=order.order_id,
                    symbol=self.symbol,
                    status=OrderStatus.FILLED,
                    accepted_quantity=order.quantity,
                    remaining_quantity=0,
                    filled_quantity=filled_quantity,
                    sequence=self._event_sequence,
                    timestamp_ns=timestamp_ns,
                )
            )
        elif order.order_type is OrderType.MARKET or order.time_in_force is TimeInForce.IOC:
            status = OrderStatus.PARTIALLY_FILLED if filled_quantity > 0 else OrderStatus.CANCELED
            acks.append(
                OrderAck(
                    order_id=order.order_id,
                    symbol=self.symbol,
                    status=status,
                    accepted_quantity=order.quantity,
                    remaining_quantity=0,
                    filled_quantity=filled_quantity,
                    sequence=self._event_sequence,
                    timestamp_ns=timestamp_ns,
                    reason="unfilled IOC quantity canceled",
                )
            )
        else:
            resting_order = self._add_resting(order, remaining, self._event_sequence)
            self._orders[order.order_id] = resting_order
            acks.append(
                OrderAck(
                    order_id=order.order_id,
                    symbol=self.symbol,
                    status=OrderStatus.RESTING,
                    accepted_quantity=order.quantity,
                    remaining_quantity=remaining,
                    filled_quantity=filled_quantity,
                    queue_position=self.queue_position(order.order_id),
                    sequence=self._event_sequence,
                    timestamp_ns=timestamp_ns,
                )
            )

        return OrderBookResult(
            acks=acks,
            fills=fills,
            snapshot=self.snapshot(timestamp_ns=timestamp_ns),
        )

    def cancel(self, request: CancelRequest) -> OrderBookResult:
        """Cancel a resting order without moving other queue positions."""

        self._event_sequence += 1
        timestamp_ns = request.timestamp_ns or self._clock()
        resting = self._orders.get(request.order_id)
        if resting is None:
            return self._reject(request.order_id, request.symbol, "unknown order id", timestamp_ns)
        if resting.order.trader_id != request.trader_id:
            return self._reject(
                request.order_id,
                request.symbol,
                "trader does not own order",
                timestamp_ns,
            )

        self._remove_resting(resting)
        ack = OrderAck(
            order_id=request.order_id,
            symbol=self.symbol,
            status=OrderStatus.CANCELED,
            accepted_quantity=resting.order.quantity,
            remaining_quantity=0,
            filled_quantity=resting.order.quantity - resting.remaining_quantity,
            sequence=self._event_sequence,
            timestamp_ns=timestamp_ns,
        )
        return OrderBookResult(
            acks=[ack],
            fills=[],
            snapshot=self.snapshot(timestamp_ns=timestamp_ns),
        )

    def amend(self, request: AmendRequest) -> OrderBookResult:
        """Cancel-replace a resting order and intentionally reset queue priority."""

        self._event_sequence += 1
        timestamp_ns = request.timestamp_ns or self._clock()
        resting = self._orders.get(request.order_id)
        if resting is None:
            return self._reject(request.order_id, request.symbol, "unknown order id", timestamp_ns)
        if resting.order.trader_id != request.trader_id:
            return self._reject(
                request.order_id,
                request.symbol,
                "trader does not own order",
                timestamp_ns,
            )

        old_remaining = resting.remaining_quantity
        self._remove_resting(resting)
        amended_order = resting.order.model_copy(
            update={
                "quantity": request.new_quantity or old_remaining,
                "price": (
                    request.new_price if request.new_price is not None else resting.order.price
                ),
                "timestamp_ns": timestamp_ns,
            }
        )
        amend_ack = OrderAck(
            order_id=request.order_id,
            symbol=self.symbol,
            status=OrderStatus.AMENDED,
            accepted_quantity=amended_order.quantity,
            remaining_quantity=amended_order.quantity,
            sequence=self._event_sequence,
            timestamp_ns=timestamp_ns,
        )
        self._seen_order_ids.discard(request.order_id)
        replacement = self.submit(amended_order)
        return OrderBookResult(
            acks=[amend_ack, *replacement.acks],
            fills=replacement.fills,
            snapshot=replacement.snapshot,
        )

    @property
    def best_bid_price(self) -> Price | None:
        """Highest resting buy price."""

        return max(self._bids) if self._bids else None

    @property
    def best_ask_price(self) -> Price | None:
        """Lowest resting sell price."""

        return min(self._asks) if self._asks else None

    def queue_position(self, order_id: OrderId) -> Qty | None:
        """Return units ahead of an order at its price level."""

        resting = self._orders.get(order_id)
        if resting is None:
            return None
        book = self._bids if resting.order.side is Side.BUY else self._asks
        queue = book[resting.order.price]
        ahead = 0
        for candidate in queue:
            if candidate.order.order_id == order_id:
                return ahead
            ahead += candidate.remaining_quantity
        return None

    def snapshot(
        self,
        *,
        depth: int | None = None,
        timestamp_ns: Nanos | None = None,
    ) -> BookSnapshot:
        """Return aggregated book depth sorted by price priority."""

        return BookSnapshot(
            symbol=self.symbol,
            bids=self._levels(self._bids, reverse=True, depth=depth),
            asks=self._levels(self._asks, reverse=False, depth=depth),
            sequence=self._event_sequence,
            timestamp_ns=timestamp_ns if timestamp_ns is not None else self._clock(),
        )

    def validate_invariants(self) -> BookInvariantReport:
        """Audit internal book indexes, queues, and crossed-market invariants.

        This method is intentionally explicit and opt-in. It is useful in tests,
        replay audits, and teaching sessions, while the matching hot path stays
        focused on deterministic price-time behavior.
        """

        active_orders: dict[str, RestingOrder] = {}
        total_bid_quantity = self._validate_book_side(self._bids, Side.BUY, active_orders)
        total_ask_quantity = self._validate_book_side(self._asks, Side.SELL, active_orders)

        indexed_order_ids = set(self._orders)
        queued_order_ids = set(active_orders)
        if indexed_order_ids != queued_order_ids:
            missing = sorted(queued_order_ids - indexed_order_ids)
            stale = sorted(indexed_order_ids - queued_order_ids)
            raise BookInvariantViolation(
                f"order index mismatch for {self.symbol}: missing={missing}, stale={stale}"
            )
        for order_id, resting in self._orders.items():
            if active_orders[order_id] is not resting:
                raise BookInvariantViolation(f"order index points to wrong queue node: {order_id}")
        if not indexed_order_ids.issubset(self._seen_order_ids):
            unseen = sorted(indexed_order_ids - self._seen_order_ids)
            raise BookInvariantViolation(f"active order ids missing from duplicate guard: {unseen}")
        if self.best_bid_price is not None and self.best_ask_price is not None:
            if self.best_bid_price >= self.best_ask_price:
                raise BookInvariantViolation(
                    f"crossed book for {self.symbol}: bid={self.best_bid_price}, "
                    f"ask={self.best_ask_price}"
                )

        return BookInvariantReport(
            symbol=self.symbol,
            active_orders=len(indexed_order_ids),
            bid_levels=len(self._bids),
            ask_levels=len(self._asks),
            total_bid_quantity=total_bid_quantity,
            total_ask_quantity=total_ask_quantity,
        )

    def _reject(
        self,
        order_id: OrderId,
        symbol: str,
        reason: str,
        timestamp_ns: Nanos,
    ) -> OrderBookResult:
        ack = OrderAck(
            order_id=order_id,
            symbol=symbol,
            status=OrderStatus.REJECTED,
            sequence=self._event_sequence,
            timestamp_ns=timestamp_ns,
            reason=reason,
        )
        return OrderBookResult(
            acks=[ack],
            fills=[],
            snapshot=self.snapshot(timestamp_ns=timestamp_ns),
        )

    def _can_match(self, side: Side, price: Price | None, order_type: OrderType) -> bool:
        if side is Side.BUY:
            best_ask = self.best_ask_price
            if best_ask is None:
                return False
            return order_type is OrderType.MARKET or (price is not None and price >= best_ask)
        best_bid = self.best_bid_price
        if best_bid is None:
            return False
        return order_type is OrderType.MARKET or (price is not None and price <= best_bid)

    def _add_resting(self, order: Order, remaining: Qty, arrival_sequence: int) -> RestingOrder:
        if order.price is None:
            raise ValueError("only priced orders can rest")
        resting = RestingOrder(
            order=order,
            remaining_quantity=remaining,
            arrival_sequence=arrival_sequence,
        )
        book = self._bids if order.side is Side.BUY else self._asks
        book.setdefault(order.price, deque()).append(resting)
        return resting

    def _remove_resting(self, resting: RestingOrder) -> None:
        price = resting.order.price
        if price is None:
            return
        book = self._bids if resting.order.side is Side.BUY else self._asks
        queue = book.get(price)
        if queue is None:
            return
        for index, candidate in enumerate(queue):
            if candidate.order.order_id == resting.order.order_id:
                del queue[index]
                break
        if not queue:
            del book[price]
        self._orders.pop(resting.order.order_id, None)

    def _make_fill(
        self,
        taker: Order,
        maker: Order,
        price: Price,
        quantity: Qty,
    ) -> Fill:
        self._trade_sequence += 1
        buyer_order_id = taker.order_id if taker.side is Side.BUY else maker.order_id
        seller_order_id = taker.order_id if taker.side is Side.SELL else maker.order_id
        return Fill(
            fill_id=f"{self.symbol}-{self._trade_sequence:012d}",
            symbol=self.symbol,
            price=price,
            quantity=quantity,
            maker_order_id=maker.order_id,
            taker_order_id=taker.order_id,
            buyer_order_id=buyer_order_id,
            seller_order_id=seller_order_id,
            maker_trader_id=maker.trader_id,
            taker_trader_id=taker.trader_id,
            sequence=self._trade_sequence,
            timestamp_ns=self._clock(),
        )

    @staticmethod
    def _levels(
        book: BookSide,
        *,
        reverse: bool,
        depth: int | None,
    ) -> list[BookLevel]:
        prices = sorted(book.keys(), reverse=reverse)
        if depth is not None:
            prices = prices[:depth]
        return [
            BookLevel(
                price=price,
                quantity=sum(order.remaining_quantity for order in book[price]),
                order_count=len(book[price]),
            )
            for price in prices
        ]

    def _validate_book_side(
        self,
        book: BookSide,
        side: Side,
        active_orders: dict[str, RestingOrder],
    ) -> Qty:
        total_quantity = 0
        for price, queue in book.items():
            if not queue:
                raise BookInvariantViolation(f"empty {side} price level: {price}")
            previous_arrival_sequence = -1
            for resting in queue:
                order = resting.order
                if order.order_id in active_orders:
                    raise BookInvariantViolation(f"duplicate queued order id: {order.order_id}")
                if order.symbol != self.symbol:
                    raise BookInvariantViolation(
                        f"queued order {order.order_id} has symbol {order.symbol}, "
                        f"expected {self.symbol}"
                    )
                if order.side is not side:
                    raise BookInvariantViolation(
                        f"queued order {order.order_id} has side {order.side}, expected {side}"
                    )
                if order.price != price:
                    raise BookInvariantViolation(
                        f"queued order {order.order_id} has price {order.price}, "
                        f"expected level {price}"
                    )
                if resting.remaining_quantity <= 0:
                    raise BookInvariantViolation(
                        f"queued order {order.order_id} has non-positive remaining quantity"
                    )
                if resting.remaining_quantity > order.quantity:
                    raise BookInvariantViolation(
                        f"queued order {order.order_id} remaining quantity exceeds original"
                    )
                if resting.arrival_sequence < previous_arrival_sequence:
                    raise BookInvariantViolation(
                        f"FIFO sequence regression at {side} level {price}: {order.order_id}"
                    )
                previous_arrival_sequence = resting.arrival_sequence
                active_orders[order.order_id] = resting
                total_quantity += resting.remaining_quantity
        return total_quantity
