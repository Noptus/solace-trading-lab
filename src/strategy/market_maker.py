"""Deterministic toy market maker.

The goal is educational, not profitable trading.  The strategy quotes around
the observed mid price and skews quotes away from inventory so learners can see
how fills, inventory, and spread capture interact.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from common.models import MarketData, MarketDataKind, Order, OrderType, Side, TimeInForce
from strategy.base import Strategy


class MarketMaker(Strategy):
    """Quote a fixed spread around L1 mid and apply a simple inventory skew."""

    def __init__(
        self,
        strategy_id: str,
        symbol: str,
        *,
        quote_size: int = 10,
        spread: Decimal = Decimal("0.10"),
        tick_size: Decimal = Decimal("0.01"),
        inventory_skew: Decimal = Decimal("0.01"),
        max_inventory: int = 100,
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id, symbol, **kwargs)
        self.quote_size = quote_size
        self.spread = spread
        self.tick_size = tick_size
        self.inventory_skew = inventory_skew
        self.max_inventory = max_inventory
        self._order_sequence = 0

    def on_market_data(self, data: MarketData) -> list[Order]:
        """Generate bid/ask orders from L1 data only."""

        if data.kind is not MarketDataKind.L1 or data.symbol != self.symbol:
            return []
        bid_price = data.payload.get("bid_price")
        ask_price = data.payload.get("ask_price")
        if bid_price is None or ask_price is None:
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

        orders: list[Order] = []
        timestamp_ns = int(data.timestamp_ns)
        if self.position.inventory < self.max_inventory:
            orders.append(self._new_limit(Side.BUY, quote_bid, timestamp_ns))
        if self.position.inventory > -self.max_inventory:
            orders.append(self._new_limit(Side.SELL, quote_ask, timestamp_ns))
        return orders

    def _new_limit(self, side: Side, price: Decimal, timestamp_ns: int) -> Order:
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

    def _floor_to_tick(self, value: Decimal) -> Decimal:
        ticks = (value / self.tick_size).to_integral_value(rounding=ROUND_FLOOR)
        return (ticks * self.tick_size).quantize(self.tick_size, rounding=ROUND_HALF_UP)

    def _ceil_to_tick(self, value: Decimal) -> Decimal:
        ticks = (value / self.tick_size).to_integral_value(rounding=ROUND_FLOOR)
        if ticks * self.tick_size < value:
            ticks += 1
        return (ticks * self.tick_size).quantize(self.tick_size, rounding=ROUND_HALF_UP)
