"""Network topology and co-location modeling for exchange connectivity.

Explores configurable co-location and connectivity assumptions. Includes latency modeling,
smart order routing with latency-adjusted fair value, and physical
cross-connect asymmetry simulation.

Illustrative configured latencies (one-way; not measured here):
- London ↔ Frankfurt: ~4.2ms (fiber), ~3.8ms (microwave)
- Chicago ↔ New York: ~6.5ms (fiber), ~4.1ms (microwave)  
- London ↔ New York: ~32ms (subsea fiber)
- London ↔ Chicago: ~38ms (fiber)
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


# =============================================================================
# CoLocationTier
# =============================================================================


class CoLocationTier(Enum):
    """Co-location proximity tiers with associated latency ranges.

    Each tier represents a physical distance category between a participant's
    infrastructure and the exchange matching engine.
    """

    SAME_RACK = auto()          # Sub-microsecond: < 1,000 ns
    SAME_CAGE = auto()          # 1-5 microseconds: 1,000 - 5,000 ns
    SAME_DC = auto()            # 5-50 microseconds: 5,000 - 50,000 ns
    CROSS_CONNECT = auto()      # 50-200 microseconds: 50,000 - 200,000 ns
    METRO = auto()              # 0.5-5 ms: 500,000 - 5,000,000 ns
    INTERCONTINENTAL = auto()   # 10-100 ms: 10,000,000 - 100,000,000 ns

    @property
    def latency_range_ns(self) -> tuple[int, int]:
        """Return (min_ns, max_ns) for this co-location tier."""
        ranges: dict[CoLocationTier, tuple[int, int]] = {
            CoLocationTier.SAME_RACK: (100, 1_000),
            CoLocationTier.SAME_CAGE: (1_000, 5_000),
            CoLocationTier.SAME_DC: (5_000, 50_000),
            CoLocationTier.CROSS_CONNECT: (50_000, 200_000),
            CoLocationTier.METRO: (500_000, 5_000_000),
            CoLocationTier.INTERCONTINENTAL: (10_000_000, 100_000_000),
        }
        return ranges[self]

    @property
    def typical_latency_ns(self) -> int:
        """Return a typical (median) latency for this tier in nanoseconds."""
        lo, hi = self.latency_range_ns
        return (lo + hi) // 2


# =============================================================================
# NetworkLink
# =============================================================================


@dataclass(frozen=True, slots=True)
class NetworkLink:
    """A directional network link between two endpoints.

    Models the physical and logical properties of a network connection
    including fiber propagation delay, serialization delay, and jitter.

    Attributes:
        source: Origin endpoint identifier.
        destination: Target endpoint identifier.
        tier: Co-location tier classification.
        base_latency_ns: Minimum achievable latency in nanoseconds.
        jitter_ns: Peak-to-peak jitter in nanoseconds (uniform distribution).
        bandwidth_gbps: Link bandwidth in gigabits per second.
        fiber_km: Physical fiber length in kilometers.
        propagation_delay_ns: Fiber propagation delay (5 ns/m = 5,000 ns/km).
    """

    source: str
    destination: str
    tier: CoLocationTier
    base_latency_ns: int
    jitter_ns: int = 0
    bandwidth_gbps: float = 10.0
    fiber_km: float = 0.0
    propagation_delay_ns: int = 0

    def __post_init__(self) -> None:
        """Compute propagation delay from fiber length if not explicitly set."""
        if self.fiber_km > 0 and self.propagation_delay_ns == 0:
            # 5 ns per meter in single-mode fiber (refractive index ~1.47)
            delay = int(self.fiber_km * 1_000 * 5)
            object.__setattr__(self, "propagation_delay_ns", delay)

    def transit_time_ns(self, payload_bytes: int = 64) -> int:
        """Calculate total transit time for a given payload size.

        Components:
        1. Serialization delay: time to push bits onto the wire
        2. Propagation delay: speed-of-light in fiber
        3. Base processing latency: switch/NIC processing
        4. Jitter: random variation (uniform distribution)

        Args:
            payload_bytes: Size of the network payload in bytes.

        Returns:
            Total one-way transit time in nanoseconds.
        """
        # Serialization delay: bytes * 8 bits / bandwidth
        # bandwidth_gbps * 1e9 bits/s -> ns per bit = 1 / bandwidth_gbps
        serialization_ns = int((payload_bytes * 8) / self.bandwidth_gbps)

        # Propagation delay (fiber or intra-DC)
        propagation = self.propagation_delay_ns

        # Base processing latency (NIC, switch ASIC, kernel bypass overhead)
        processing = self.base_latency_ns

        # Jitter: uniform random within ±jitter/2
        jitter = random.randint(0, self.jitter_ns) if self.jitter_ns > 0 else 0

        return serialization_ns + propagation + processing + jitter


# =============================================================================
# Venue & Participant Models
# =============================================================================


@dataclass(slots=True)
class Venue:
    """An exchange venue with geographic location.

    Attributes:
        name: Exchange identifier (e.g., 'LSE', 'CME').
        location: Geographic location (e.g., 'London', 'Chicago').
        matching_engine_latency_ns: Internal ME processing time.
    """

    name: str
    location: str
    matching_engine_latency_ns: int = 500  # Typical FPGA-based ME


@dataclass(slots=True)
class Participant:
    """A market participant co-located at a venue.

    Attributes:
        name: Participant identifier.
        venue: The venue where co-located.
        colo_tier: Physical proximity tier.
        link: Network link from participant to venue matching engine.
    """

    name: str
    venue: str
    colo_tier: CoLocationTier
    link: NetworkLink


# =============================================================================
# ExchangeTopology
# =============================================================================


class ExchangeTopology:
    """Models multi-venue exchange topology with realistic inter-venue latencies.

    Provides graph-based routing between venues and participants with
    Dijkstra shortest-path computation for optimal order routing.

    Example:
        >>> topo = ExchangeTopology.build_default()
        >>> matrix = topo.latency_matrix()
        >>> path = topo.fastest_path('LSE', 'CME')
    """

    def __init__(self) -> None:
        """Initialize an empty topology."""
        self._venues: dict[str, Venue] = {}
        self._participants: dict[str, Participant] = {}
        self._links: list[NetworkLink] = []
        # Adjacency list: node -> [(neighbor, weight_ns)]
        self._graph: dict[str, list[tuple[str, int]]] = {}

    def add_venue(self, name: str, location: str, me_latency_ns: int = 500) -> Venue:
        """Add an exchange venue to the topology.

        Args:
            name: Unique venue identifier (e.g., 'LSE').
            location: Geographic location (e.g., 'London').
            me_latency_ns: Matching engine processing latency.

        Returns:
            The created Venue instance.

        Raises:
            ValueError: If venue name already exists.
        """
        if name in self._venues:
            raise ValueError(f"Venue '{name}' already exists in topology")
        venue = Venue(name=name, location=location, matching_engine_latency_ns=me_latency_ns)
        self._venues[name] = venue
        if name not in self._graph:
            self._graph[name] = []
        return venue

    def add_link(self, link: NetworkLink) -> None:
        """Add a network link between two nodes in the topology.

        Creates bidirectional edges in the routing graph.

        Args:
            link: The NetworkLink to add.
        """
        self._links.append(link)
        weight = link.transit_time_ns(payload_bytes=64)

        if link.source not in self._graph:
            self._graph[link.source] = []
        if link.destination not in self._graph:
            self._graph[link.destination] = []

        self._graph[link.source].append((link.destination, weight))
        self._graph[link.destination].append((link.source, weight))

    def add_participant(
        self,
        name: str,
        colo_tier: CoLocationTier,
        venue: str,
    ) -> Participant:
        """Add a market participant co-located at a specific venue.

        Creates a network link from participant to venue based on the
        co-location tier's typical latency characteristics.

        Args:
            name: Unique participant identifier.
            colo_tier: Physical proximity to the venue's matching engine.
            venue: Name of the venue where participant is co-located.

        Returns:
            The created Participant instance.

        Raises:
            ValueError: If venue doesn't exist or participant already registered.
        """
        if venue not in self._venues:
            raise ValueError(f"Venue '{venue}' not found. Add it first.")
        if name in self._participants:
            raise ValueError(f"Participant '{name}' already exists")

        lo, hi = colo_tier.latency_range_ns
        base_latency = (lo + hi) // 2
        jitter = (hi - lo) // 4  # ~25% of range as jitter

        link = NetworkLink(
            source=name,
            destination=venue,
            tier=colo_tier,
            base_latency_ns=base_latency,
            jitter_ns=jitter,
            bandwidth_gbps=100.0 if colo_tier in (
                CoLocationTier.SAME_RACK, CoLocationTier.SAME_CAGE
            ) else 10.0,
        )

        participant = Participant(
            name=name,
            venue=venue,
            colo_tier=colo_tier,
            link=link,
        )
        self._participants[name] = participant

        # Add to graph
        if name not in self._graph:
            self._graph[name] = []
        weight = link.transit_time_ns(payload_bytes=64)
        self._graph[name].append((venue, weight))
        self._graph[venue].append((name, weight))

        return participant

    def latency_matrix(self) -> dict[str, dict[str, int]]:
        """Compute venue-to-venue latency matrix using shortest paths.

        Returns:
            Nested dict mapping source venue -> destination venue -> latency_ns.
            Diagonal entries are the matching engine's internal latency.
        """
        venue_names = list(self._venues.keys())
        matrix: dict[str, dict[str, int]] = {}

        for src in venue_names:
            matrix[src] = {}
            distances = self._dijkstra(src)
            for dst in venue_names:
                if src == dst:
                    matrix[src][dst] = self._venues[src].matching_engine_latency_ns
                else:
                    matrix[src][dst] = distances.get(dst, -1)

        return matrix

    def fastest_path(self, source: str, dest: str) -> tuple[list[str], int]:
        """Find the fastest path between any two nodes using Dijkstra's algorithm.

        Args:
            source: Starting node (venue or participant name).
            dest: Target node (venue or participant name).

        Returns:
            Tuple of (path as list of node names, total latency in nanoseconds).

        Raises:
            ValueError: If source or destination not in topology.
            RuntimeError: If no path exists between source and dest.
        """
        if source not in self._graph:
            raise ValueError(f"Node '{source}' not found in topology")
        if dest not in self._graph:
            raise ValueError(f"Node '{dest}' not found in topology")

        # Dijkstra with path reconstruction
        dist: dict[str, int] = {source: 0}
        prev: dict[str, str | None] = {source: None}
        pq: list[tuple[int, str]] = [(0, source)]

        while pq:
            d, u = heapq.heappop(pq)
            if u == dest:
                break
            if d > dist.get(u, float("inf")):  # type: ignore[arg-type]
                continue
            for neighbor, weight in self._graph.get(u, []):
                new_dist = d + weight
                if new_dist < dist.get(neighbor, float("inf")):  # type: ignore[arg-type]
                    dist[neighbor] = new_dist
                    prev[neighbor] = u
                    heapq.heappush(pq, (new_dist, neighbor))

        if dest not in dist:
            raise RuntimeError(f"No path from '{source}' to '{dest}'")

        # Reconstruct path
        path: list[str] = []
        node: str | None = dest
        while node is not None:
            path.append(node)
            node = prev.get(node)
        path.reverse()

        return path, dist[dest]

    def _dijkstra(self, source: str) -> dict[str, int]:
        """Run Dijkstra's algorithm from a source node.

        Args:
            source: Starting node.

        Returns:
            Dict mapping each reachable node to its shortest distance in ns.
        """
        dist: dict[str, int] = {source: 0}
        pq: list[tuple[int, str]] = [(0, source)]

        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):  # type: ignore[arg-type]
                continue
            for neighbor, weight in self._graph.get(u, []):
                new_dist = d + weight
                if new_dist < dist.get(neighbor, float("inf")):  # type: ignore[arg-type]
                    dist[neighbor] = new_dist
                    heapq.heappush(pq, (new_dist, neighbor))

        return dist

    @classmethod
    def build_default(cls) -> ExchangeTopology:
        """Build the default global exchange topology.

        Pre-built connectivity:
        - London: LSE, ICE Europe
        - Frankfurt: Eurex
        - Chicago: CME
        - New York: NASDAQ, NYSE

        Inter-venue links use realistic fiber latencies:
        - London ↔ Frankfurt: ~4.2ms (800km fiber)
        - Frankfurt ↔ Chicago: ~42ms (8,500km fiber via submarine)
        - Chicago ↔ New York: ~6.5ms (1,300km fiber)
        - London ↔ New York: ~32ms (6,400km submarine fiber)

        Returns:
            Fully connected ExchangeTopology instance.
        """
        topo = cls()

        # Add venues
        topo.add_venue("LSE", "London", me_latency_ns=450)
        topo.add_venue("ICE_EU", "London", me_latency_ns=500)
        topo.add_venue("Eurex", "Frankfurt", me_latency_ns=400)
        topo.add_venue("CME", "Chicago", me_latency_ns=350)
        topo.add_venue("NASDAQ", "New York", me_latency_ns=500)
        topo.add_venue("NYSE", "New York", me_latency_ns=550)

        # London intra-city: LSE ↔ ICE (same data center complex, ~2km fiber)
        topo.add_link(NetworkLink(
            source="LSE",
            destination="ICE_EU",
            tier=CoLocationTier.SAME_DC,
            base_latency_ns=5_000,
            jitter_ns=500,
            bandwidth_gbps=100.0,
            fiber_km=2.0,
        ))

        # New York intra-city: NASDAQ ↔ NYSE (~5km fiber, Carteret ↔ Mahwah)
        topo.add_link(NetworkLink(
            source="NASDAQ",
            destination="NYSE",
            tier=CoLocationTier.SAME_DC,
            base_latency_ns=10_000,
            jitter_ns=2_000,
            bandwidth_gbps=100.0,
            fiber_km=5.0,
        ))

        # London ↔ Frankfurt: ~800km fiber, ~4.2ms one-way
        topo.add_link(NetworkLink(
            source="LSE",
            destination="Eurex",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=200_000,
            jitter_ns=50_000,
            bandwidth_gbps=10.0,
            fiber_km=800.0,
        ))

        topo.add_link(NetworkLink(
            source="ICE_EU",
            destination="Eurex",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=200_000,
            jitter_ns=50_000,
            bandwidth_gbps=10.0,
            fiber_km=800.0,
        ))

        # Frankfurt ↔ Chicago: ~8,500km (submarine + terrestrial), ~42ms
        topo.add_link(NetworkLink(
            source="Eurex",
            destination="CME",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=500_000,
            jitter_ns=200_000,
            bandwidth_gbps=10.0,
            fiber_km=8_500.0,
        ))

        # Chicago ↔ New York: ~1,300km fiber, ~6.5ms
        topo.add_link(NetworkLink(
            source="CME",
            destination="NASDAQ",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=150_000,
            jitter_ns=30_000,
            bandwidth_gbps=40.0,
            fiber_km=1_300.0,
        ))

        topo.add_link(NetworkLink(
            source="CME",
            destination="NYSE",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=150_000,
            jitter_ns=30_000,
            bandwidth_gbps=40.0,
            fiber_km=1_350.0,
        ))

        # London ↔ New York: ~6,400km submarine fiber, ~32ms
        topo.add_link(NetworkLink(
            source="LSE",
            destination="NASDAQ",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=300_000,
            jitter_ns=100_000,
            bandwidth_gbps=10.0,
            fiber_km=6_400.0,
        ))

        topo.add_link(NetworkLink(
            source="LSE",
            destination="NYSE",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=300_000,
            jitter_ns=100_000,
            bandwidth_gbps=10.0,
            fiber_km=6_400.0,
        ))

        return topo


# =============================================================================
# SmartOrderRouter
# =============================================================================


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """Snapshot of a venue's order book for routing decisions.

    Attributes:
        venue: Venue name.
        best_bid: Best bid price.
        best_ask: Best ask price.
        bid_size: Size at best bid.
        ask_size: Size at best ask.
        queue_position_estimate: Estimated queue position (0 = front).
        last_update_ns: Timestamp of last update in nanoseconds.
    """

    venue: str
    best_bid: float
    best_ask: float
    bid_size: int = 0
    ask_size: int = 0
    queue_position_estimate: int = 0
    last_update_ns: int = 0


@dataclass(frozen=True, slots=True)
class Order:
    """A trading order to be routed.

    Attributes:
        symbol: Instrument identifier.
        side: 'buy' or 'sell'.
        quantity: Order size.
        limit_price: Maximum (buy) or minimum (sell) acceptable price.
        order_type: 'limit', 'market', or 'ioc'.
    """

    symbol: str
    side: str  # 'buy' | 'sell'
    quantity: int
    limit_price: float | None = None
    order_type: str = "limit"  # 'limit' | 'market' | 'ioc'


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Result of smart order routing decision.

    Attributes:
        venue: Selected venue.
        expected_latency_ns: Expected tick-to-trade latency.
        expected_fill_price: Expected fill price after latency adjustment.
        confidence: Routing confidence score (0.0 - 1.0).
    """

    venue: str
    expected_latency_ns: int
    expected_fill_price: float
    confidence: float


@dataclass(frozen=True, slots=True)
class SprayAllocation:
    """Allocation for IOC spray across venues.

    Attributes:
        venue: Target venue.
        quantity: Allocated quantity.
        expected_latency_ns: Expected latency to this venue.
    """

    venue: str
    quantity: int
    expected_latency_ns: int


class SmartOrderRouter:
    """Latency-aware smart order router across multiple venues.

    Routes orders based on:
    1. Best available price across venues
    2. Network latency to each venue (tick-to-trade)
    3. Queue position estimates
    4. Latency-adjusted fair value calculation

    Routing heuristic: prefer the venue where the model gives the lowest
    tick-to-trade latency, accepting slightly worse prices if the
    latency advantage provides edge on expected price movement.

    Example:
        >>> router = SmartOrderRouter(topology, participant='research-client')
        >>> decision = router.route(order, books)
        >>> allocations = router.spray(order, ['CME', 'NASDAQ'], [0.6, 0.4])
    """

    def __init__(
        self,
        topology: ExchangeTopology,
        participant: str,
        tick_value_bps: float = 0.5,
        latency_alpha: float = 0.001,
    ) -> None:
        """Initialize the smart order router.

        Args:
            topology: The exchange topology for latency lookups.
            participant: Name of the participant (must exist in topology).
            tick_value_bps: Value of one tick in basis points for latency adjustment.
            latency_alpha: Weight for latency penalty in routing score.
        """
        self._topology = topology
        self._participant = participant
        self._tick_value_bps = tick_value_bps
        self._latency_alpha = latency_alpha
        self._venue_latencies: dict[str, int] = {}
        self._precompute_latencies()

    def _precompute_latencies(self) -> None:
        """Precompute latencies from participant to all reachable venues."""
        for venue_name in self._topology._venues:
            try:
                _, latency = self._topology.fastest_path(self._participant, venue_name)
                self._venue_latencies[venue_name] = latency
            except (ValueError, RuntimeError):
                pass  # Venue not reachable

    def latency_to_venue(self, venue: str) -> int:
        """Get precomputed latency to a specific venue.

        Args:
            venue: Venue name.

        Returns:
            One-way latency in nanoseconds, or -1 if unreachable.
        """
        return self._venue_latencies.get(venue, -1)

    def latency_adjusted_fair_value(
        self,
        mid_price: float,
        venue: str,
        price_velocity_per_ns: float = 0.0,
    ) -> float:
        """Calculate latency-adjusted fair value for a venue.

        Adjusts the current mid-price by the expected price movement
        during the network transit time to the venue.

        Args:
            mid_price: Current mid-price estimate.
            venue: Target venue.
            price_velocity_per_ns: Expected price drift per nanosecond.

        Returns:
            Fair value adjusted for latency to venue.
        """
        latency = self._venue_latencies.get(venue, 0)
        return mid_price + (price_velocity_per_ns * latency)

    def route(
        self,
        order: Order,
        books: dict[str, OrderBookSnapshot],
    ) -> RoutingDecision:
        """Route an order to the optimal venue.

        Scoring formula (for a buy order):
        score = -price_score + latency_penalty + queue_penalty

        Where:
        - price_score: How good the price is (lower ask = better for buys)
        - latency_penalty: alpha * latency_ns (prefer low-latency venues)
        - queue_penalty: Estimated time-in-queue cost

        Jump style: A venue with 100ns less latency can justify paying
        ~0.5bps more, as the speed advantage captures price movements.

        Args:
            order: The order to route.
            books: Dict mapping venue name to OrderBookSnapshot.

        Returns:
            RoutingDecision with venue, latency, expected price, and confidence.

        Raises:
            ValueError: If no valid venue found for the order.
        """
        if not books:
            raise ValueError("No order book snapshots provided")

        candidates: list[tuple[float, str, float, int]] = []  # (score, venue, price, latency)

        for venue_name, snapshot in books.items():
            latency = self._venue_latencies.get(venue_name, -1)
            if latency < 0:
                continue  # Venue unreachable

            # Determine relevant price
            if order.side == "buy":
                price = snapshot.best_ask
                # Lower ask is better for buyer
                if order.limit_price is not None and price > order.limit_price:
                    continue  # Price exceeds limit
            else:
                price = snapshot.best_bid
                # Higher bid is better for seller
                if order.limit_price is not None and price < order.limit_price:
                    continue  # Price below limit

            # Price score: normalize relative to best available
            price_score = price if order.side == "buy" else -price

            # Latency penalty: each nanosecond costs alpha in price units
            latency_penalty = self._latency_alpha * latency / 1_000_000  # Normalize to ms

            # Queue position penalty: estimated fill probability
            queue_penalty = snapshot.queue_position_estimate * 0.01

            # Combined score (lower is better for buys)
            score = price_score + latency_penalty + queue_penalty

            candidates.append((score, venue_name, price, latency))

        if not candidates:
            raise ValueError("No valid venue found for order routing")

        # Sort by score (lowest = best for buys, need to invert for sells)
        candidates.sort(key=lambda x: x[0])

        best_score, best_venue, best_price, best_latency = candidates[0]

        # Confidence based on score separation from next-best
        confidence = 1.0
        if len(candidates) > 1:
            score_gap = abs(candidates[1][0] - candidates[0][0])
            confidence = min(1.0, score_gap / (abs(candidates[0][0]) + 1e-10))
            confidence = max(0.1, min(1.0, confidence))

        return RoutingDecision(
            venue=best_venue,
            expected_latency_ns=best_latency,
            expected_fill_price=best_price,
            confidence=confidence,
        )

    def spray(
        self,
        order: Order,
        venues: list[str],
        ratios: list[float],
    ) -> list[SprayAllocation]:
        """Spray an IOC order across multiple venues simultaneously.

        Distributes the order quantity across venues according to specified
        ratios, used for aggressive taking when liquidity is fragmented.

        Args:
            order: The order to spray (typically IOC type).
            venues: List of target venue names.
            ratios: Allocation ratios (must sum to ~1.0).

        Returns:
            List of SprayAllocation objects with per-venue quantities.

        Raises:
            ValueError: If venues and ratios have different lengths.
        """
        if len(venues) != len(ratios):
            raise ValueError("venues and ratios must have same length")

        # Normalize ratios
        total_ratio = sum(ratios)
        normalized = [r / total_ratio for r in ratios]

        allocations: list[SprayAllocation] = []
        remaining = order.quantity

        for i, (venue, ratio) in enumerate(zip(venues, normalized)):
            if i == len(venues) - 1:
                # Last venue gets remainder to avoid rounding issues
                qty = remaining
            else:
                qty = int(order.quantity * ratio)
                remaining -= qty

            latency = self._venue_latencies.get(venue, -1)
            allocations.append(SprayAllocation(
                venue=venue,
                quantity=qty,
                expected_latency_ns=latency,
            ))

        return allocations


# =============================================================================
# CrossConnect
# =============================================================================


@dataclass(slots=True)
class CrossConnect:
    """Models a physical cross-connect between participant cage and exchange ME.

    In co-location facilities, cross-connects are dedicated fiber pairs
    running from a participant's cage/rack to the exchange's matching
    engine switch. Each meter of fiber adds ~5ns of propagation delay.

    Real-world considerations:
    - Cable routing through cable trays adds 20-40% length vs straight-line
    - TX and RX paths may traverse different physical routes
    - Connector losses add ~0.1ns per mated pair (negligible)
    - Temperature affects fiber refractive index (~0.01ns/m/°C)

    Attributes:
        participant: Participant identifier.
        venue: Exchange venue identifier.
        tx_fiber_meters: Transmit path fiber length in meters.
        rx_fiber_meters: Receive path fiber length in meters.
        connector_pairs: Number of fiber connector mating pairs.
        switch_hops: Number of network switches in path.
        switch_latency_ns: Per-switch forwarding latency.
    """

    participant: str
    venue: str
    tx_fiber_meters: float
    rx_fiber_meters: float | None = None
    connector_pairs: int = 2
    switch_hops: int = 1
    switch_latency_ns: int = 80  # Modern cut-through switch

    def __post_init__(self) -> None:
        """Default RX fiber to TX fiber length if not specified."""
        if self.rx_fiber_meters is None:
            self.rx_fiber_meters = self.tx_fiber_meters

    @property
    def tx_propagation_ns(self) -> int:
        """Transmit path propagation delay in nanoseconds (5ns per meter)."""
        return int(self.tx_fiber_meters * 5)

    @property
    def rx_propagation_ns(self) -> int:
        """Receive path propagation delay in nanoseconds (5ns per meter)."""
        assert self.rx_fiber_meters is not None
        return int(self.rx_fiber_meters * 5)

    @property
    def asymmetry_ns(self) -> int:
        """Path asymmetry between TX and RX in nanoseconds.

        Non-zero asymmetry is common due to different cable routing
        for transmit vs receive paths. Typical range: 1-10ns.
        """
        return abs(self.tx_propagation_ns - self.rx_propagation_ns)

    @property
    def round_trip_ns(self) -> int:
        """Total round-trip time through the cross-connect.

        Includes TX propagation + RX propagation + switch forwarding.
        Does not include matching engine processing time.
        """
        switch_total = self.switch_hops * self.switch_latency_ns
        return self.tx_propagation_ns + self.rx_propagation_ns + (2 * switch_total)

    def one_way_latency_ns(self, direction: str = "tx") -> int:
        """Calculate one-way latency for a specific direction.

        Args:
            direction: 'tx' for participant→exchange, 'rx' for exchange→participant.

        Returns:
            One-way latency in nanoseconds including switch forwarding.

        Raises:
            ValueError: If direction is not 'tx' or 'rx'.
        """
        if direction == "tx":
            propagation = self.tx_propagation_ns
        elif direction == "rx":
            propagation = self.rx_propagation_ns
        else:
            raise ValueError(f"direction must be 'tx' or 'rx', got '{direction}'")

        switch_total = self.switch_hops * self.switch_latency_ns
        return propagation + switch_total

    def tick_to_trade_ns(self, processing_ns: int = 200) -> int:
        """Estimate total tick-to-trade latency through this cross-connect.

        Tick-to-trade = market data RX + internal processing + order TX.

        Args:
            processing_ns: Internal FPGA/software processing time.

        Returns:
            Total tick-to-trade latency in nanoseconds.
        """
        # Market data arrives on RX path, order goes out on TX path
        return self.one_way_latency_ns("rx") + processing_ns + self.one_way_latency_ns("tx")

    @classmethod
    def equalized_pair(
        cls,
        participant: str,
        venue: str,
        target_length_meters: float,
        max_asymmetry_ns: int = 5,
    ) -> CrossConnect:
        """Create a length-equalized cross-connect pair.

        Premium co-location providers offer equalized cable lengths
        to ensure fairness. This simulates slight residual asymmetry.

        Args:
            participant: Participant identifier.
            venue: Venue identifier.
            target_length_meters: Target equalized fiber length.
            max_asymmetry_ns: Maximum acceptable asymmetry in ns.

        Returns:
            CrossConnect with near-equal TX/RX paths.
        """
        # Residual asymmetry: 0-2 meters difference (0-10ns)
        max_diff_meters = max_asymmetry_ns / 5.0
        diff = random.uniform(0, max_diff_meters)

        return cls(
            participant=participant,
            venue=venue,
            tx_fiber_meters=target_length_meters,
            rx_fiber_meters=target_length_meters + diff,
        )
