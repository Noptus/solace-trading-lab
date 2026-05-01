"""Deterministic example market-making strategy."""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from common.models import (
    CancelRequest,
    MarketData,
    MarketDataKind,
    Nanos,
    Order,
    OrderType,
    Price,
    Qty,
    Side,
    TimeInForce,
)
from strategy.base import Strategy


class MarketMaker(Strategy):
    """Quote fixed spread around L1 mid with inventory skew."""

    def __init__(
        self,
        strategy_id: str,
        symbol: str,
        *,
        quote_size: Qty = 10,
        spread: Price = Decimal("0.10"),
        tick_size: Price = Decimal("0.01"),
        inventory_skew: Price = Decimal("0.01"),
        max_inventory: Qty = 100,
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id, symbol, **kwargs)
        self.quote_size = quote_size
        self.spread = spread
        self.tick_size = tick_size
        self.inventory_skew = inventory_skew
        self.max_inventory = max_inventory
        self._order_sequence = 0
        self._active_bid_id: str | None = None
        self._active_ask_id: str | None = None
        self._last_quoted_bid: Price | None = None
        self._last_quoted_ask: Price | None = None

    def on_market_data(self, data: MarketData) -> list[Order]:
        """Generate bid/ask orders from L1 data only, canceling stale quotes first."""

        if data.kind is not MarketDataKind.L1 or data.symbol != self.symbol:
            return []
        bid_price = data.payload.get("bid_price")
        ask_price = data.payload.get("ask_price")
        if bid_price is None or ask_price is None:
            return []

        if (
            self._last_quoted_bid is not None
            and Decimal(str(bid_price)) == self._last_quoted_bid
            and Decimal(str(ask_price)) == self._last_quoted_ask
        ):
            return []

        best_bid = Decimal(str(bid_price))
        best_ask = Decimal(str(ask_price))
        if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            return []

        mid = (best_bid + best_ask) / Decimal("2")
        skew = Decimal(self.position.inventory) * self.inventory_skew
        raw_bid = mid - (self.spread / Decimal("2")) - skew
        raw_ask = mid + (self.spread / Decimal("2")) - skew
        quote_bid = min(self._floor_to_tick(raw_bid), best_bid)
        quote_ask = max(self._ceil_to_tick(raw_ask), best_ask)
        if quote_bid >= quote_ask:
            quote_bid = best_bid
            quote_ask = best_ask

        self._cancel_active_quotes()

        orders: list[Order] = []
        timestamp_ns = int(data.timestamp_ns)
        if self.position.inventory < self.max_inventory:
            order = self._new_limit(Side.BUY, quote_bid, timestamp_ns)
            self._active_bid_id = order.order_id
            orders.append(order)
        if self.position.inventory > -self.max_inventory:
            order = self._new_limit(Side.SELL, quote_ask, timestamp_ns)
            self._active_ask_id = order.order_id
            orders.append(order)

        self._last_quoted_bid = quote_bid
        self._last_quoted_ask = quote_ask
        return orders

    def _cancel_active_quotes(self) -> None:
        """Cancel previously placed quotes that are still resting."""

        for order_id in (self._active_bid_id, self._active_ask_id):
            if order_id is not None:
                self.cancel_order(
                    CancelRequest(
                        order_id=order_id,
                        symbol=self.symbol,
                        trader_id=self.strategy_id,
                    )
                )
        self._active_bid_id = None
        self._active_ask_id = None

    def _new_limit(self, side: Side, price: Price, timestamp_ns: Nanos) -> Order:
        self._order_sequence += 1
        return Order(
            order_id=f"{self.strategy_id}-{self.symbol}-{self._order_sequence:06d}",
            symbol=self.symbol,
            side=side,
            quantity=self.quote_size,
            price=price,
            trader_id=self.strategy_id,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            timestamp_ns=timestamp_ns,
        )

    def _floor_to_tick(self, value: Price) -> Price:
        ticks = (value / self.tick_size).to_integral_value(rounding=ROUND_FLOOR)
        return (ticks * self.tick_size).quantize(self.tick_size, rounding=ROUND_HALF_UP)

    def _ceil_to_tick(self, value: Price) -> Price:
        ticks = (value / self.tick_size).to_integral_value(rounding=ROUND_FLOOR)
        if ticks * self.tick_size < value:
            ticks += 1
        return (ticks * self.tick_size).quantize(self.tick_size, rounding=ROUND_HALF_UP)
