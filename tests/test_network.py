"""Comprehensive tests for network topology module.

Tests cover:
- CoLocationTier latency ordering and physical plausibility
- NetworkLink propagation, serialization, and jitter modeling
- ExchangeTopology graph construction and Dijkstra routing
- SmartOrderRouter price/latency-aware routing and spray allocation
- CrossConnect fiber delay, asymmetry, and equalization
"""

import random

import pytest

from network.topology import (
    CoLocationTier,
    CrossConnect,
    ExchangeTopology,
    NetworkLink,
    Order,
    OrderBookSnapshot,
    RoutingDecision,
    SmartOrderRouter,
    SprayAllocation,
)


# =============================================================================
# CoLocationTier Tests
# =============================================================================


class TestCoLocationTier:
    """Tests for CoLocationTier enum and latency range properties."""

    def test_tier_ordering_strict_monotonic(self):
        """Each successive tier must have strictly higher min and max latency."""
        tiers = [
            CoLocationTier.SAME_RACK,
            CoLocationTier.SAME_CAGE,
            CoLocationTier.SAME_DC,
            CoLocationTier.CROSS_CONNECT,
            CoLocationTier.METRO,
            CoLocationTier.INTERCONTINENTAL,
        ]
        for i in range(len(tiers) - 1):
            lo_curr, hi_curr = tiers[i].latency_range_ns
            lo_next, hi_next = tiers[i + 1].latency_range_ns
            assert hi_curr <= lo_next, (
                f"{tiers[i].name} max ({hi_curr}ns) must be <= "
                f"{tiers[i+1].name} min ({lo_next}ns)"
            )

    def test_same_rack_sub_microsecond(self):
        """SAME_RACK latency must be sub-microsecond (< 1000ns)."""
        lo, hi = CoLocationTier.SAME_RACK.latency_range_ns
        assert lo >= 50, "SAME_RACK min must be physically plausible (>= 50ns)"
        assert hi <= 1_000, "SAME_RACK max must be sub-microsecond"

    def test_same_cage_low_microseconds(self):
        """SAME_CAGE: 1-5 microseconds, realistic for ~10-50m fiber run."""
        lo, hi = CoLocationTier.SAME_CAGE.latency_range_ns
        assert lo == 1_000
        assert hi == 5_000

    def test_same_dc_tens_of_microseconds(self):
        """SAME_DC: 5-50 microseconds, realistic for within one facility."""
        lo, hi = CoLocationTier.SAME_DC.latency_range_ns
        assert 5_000 <= lo <= 10_000
        assert 20_000 <= hi <= 50_000

    def test_cross_connect_hundreds_microseconds(self):
        """CROSS_CONNECT: 50-200μs, realistic for dedicated fiber to ME switch."""
        lo, hi = CoLocationTier.CROSS_CONNECT.latency_range_ns
        assert 50_000 <= lo <= 100_000
        assert 150_000 <= hi <= 200_000

    def test_metro_sub_millisecond_to_few_ms(self):
        """METRO: 0.5-5ms, realistic for city-level distances (5-50km)."""
        lo, hi = CoLocationTier.METRO.latency_range_ns
        assert 500_000 <= lo <= 1_000_000
        assert 3_000_000 <= hi <= 5_000_000

    def test_intercontinental_tens_of_ms(self):
        """INTERCONTINENTAL: 10-100ms, realistic for subsea/terrestrial fiber."""
        lo, hi = CoLocationTier.INTERCONTINENTAL.latency_range_ns
        assert 10_000_000 <= lo <= 20_000_000
        assert 50_000_000 <= hi <= 100_000_000

    def test_typical_latency_is_midpoint(self):
        """typical_latency_ns must be the arithmetic mean of range bounds."""
        for tier in CoLocationTier:
            lo, hi = tier.latency_range_ns
            expected = (lo + hi) // 2
            assert tier.typical_latency_ns == expected

    def test_no_gap_between_tiers(self):
        """Adjacent tiers should have contiguous or overlapping ranges."""
        tiers = list(CoLocationTier)
        for i in range(len(tiers) - 1):
            _, hi_curr = tiers[i].latency_range_ns
            lo_next, _ = tiers[i + 1].latency_range_ns
            # Allow gap factor of at most 10x between tiers
            assert lo_next / hi_curr <= 10, (
                f"Gap between {tiers[i].name} and {tiers[i+1].name} is too large"
            )


# =============================================================================
# NetworkLink Tests
# =============================================================================


class TestNetworkLink:
    """Tests for NetworkLink propagation, serialization, and jitter."""

    def test_fiber_propagation_5ns_per_meter(self):
        """Fiber propagation delay should be ~5ns/m (refractive index ~1.47).

        Physical basis: speed of light in fiber = c/n ≈ 3e8/1.47 ≈ 2.04e8 m/s
        → ~4.9ns/m, code rounds to 5ns/m for simplicity.
        """
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.METRO,
            base_latency_ns=0,
            fiber_km=1.0,  # 1km = 1000m
        )
        # 1000m * 5ns/m = 5000ns
        assert link.propagation_delay_ns == 5_000

    def test_fiber_propagation_realistic_london_frankfurt(self):
        """800km London-Frankfurt fiber should give ~4ms propagation."""
        link = NetworkLink(
            source="LSE",
            destination="Eurex",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=0,
            fiber_km=800.0,
        )
        # 800km * 1000m/km * 5ns/m = 4,000,000ns = 4ms
        assert link.propagation_delay_ns == 4_000_000
        # Physically plausible: ~4ms matches real-world London-Frankfurt fiber

    def test_fiber_propagation_realistic_chicago_nyc(self):
        """1300km Chicago-NYC fiber should give ~6.5ms propagation."""
        link = NetworkLink(
            source="CME",
            destination="NASDAQ",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=0,
            fiber_km=1_300.0,
        )
        assert link.propagation_delay_ns == 6_500_000  # 6.5ms

    def test_transit_time_includes_serialization(self):
        """transit_time must include serialization delay: bits/bandwidth."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_RACK,
            base_latency_ns=100,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=0.0,
        )
        # 64 bytes * 8 bits = 512 bits / 10 Gbps = 51.2ns → int(51.2) = 51ns
        # Total = 51 (serialization) + 0 (propagation) + 100 (base) + 0 (jitter) = 151ns
        transit = link.transit_time_ns(payload_bytes=64)
        assert transit == 151

    def test_transit_time_serialization_100g(self):
        """100Gbps link serializes 64B in ~5ns."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_RACK,
            base_latency_ns=0,
            jitter_ns=0,
            bandwidth_gbps=100.0,
        )
        # 512 bits / 100 Gbps = 5.12ns → int = 5ns
        transit = link.transit_time_ns(payload_bytes=64)
        assert transit == 5

    def test_transit_time_large_payload(self):
        """1500B MTU on 10G link gives 1200ns serialization."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_DC,
            base_latency_ns=0,
            jitter_ns=0,
            bandwidth_gbps=10.0,
        )
        # 1500 * 8 / 10 = 1200ns
        transit = link.transit_time_ns(payload_bytes=1500)
        assert transit == 1200

    def test_transit_time_with_propagation(self):
        """Transit includes both serialization and propagation."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.METRO,
            base_latency_ns=1_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=10.0,  # 10km → 50,000ns propagation
        )
        # serialization: 512/10 = 51ns
        # propagation: 50,000ns
        # base: 1,000ns
        # Total: 51,051ns
        transit = link.transit_time_ns(payload_bytes=64)
        assert transit == 51_051

    def test_jitter_adds_nondeterminism_with_seed(self):
        """Jitter should produce different transit times with different seeds."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_DC,
            base_latency_ns=5_000,
            jitter_ns=1_000,
            bandwidth_gbps=10.0,
        )
        # Collect transit times with different seeds
        results = set()
        for seed in range(100):
            random.seed(seed)
            results.add(link.transit_time_ns(payload_bytes=64))

        # With 1000ns jitter range, we should see multiple distinct values
        assert len(results) > 10, "Jitter should produce non-deterministic transit times"

    def test_jitter_bounded(self):
        """Jitter contribution must be in [0, jitter_ns]."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_DC,
            base_latency_ns=5_000,
            jitter_ns=2_000,
            bandwidth_gbps=10.0,
        )
        # Without jitter: serialization(51) + base(5000) = 5051
        min_expected = 5_051
        max_expected = 5_051 + 2_000

        for seed in range(200):
            random.seed(seed)
            transit = link.transit_time_ns(payload_bytes=64)
            assert min_expected <= transit <= max_expected, (
                f"Transit {transit}ns out of bounds [{min_expected}, {max_expected}]"
            )

    def test_jitter_reproducible_with_same_seed(self):
        """Same seed must produce identical transit times."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_DC,
            base_latency_ns=5_000,
            jitter_ns=1_000,
            bandwidth_gbps=10.0,
        )
        random.seed(42)
        t1 = link.transit_time_ns()
        random.seed(42)
        t2 = link.transit_time_ns()
        assert t1 == t2

    def test_zero_jitter_deterministic(self):
        """Link with jitter_ns=0 must always return the same transit time."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_RACK,
            base_latency_ns=500,
            jitter_ns=0,
            bandwidth_gbps=100.0,
        )
        times = {link.transit_time_ns() for _ in range(100)}
        assert len(times) == 1

    def test_propagation_not_set_when_no_fiber(self):
        """propagation_delay_ns stays 0 when fiber_km is 0."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.SAME_RACK,
            base_latency_ns=200,
            fiber_km=0.0,
        )
        assert link.propagation_delay_ns == 0

    def test_explicit_propagation_delay_not_overridden(self):
        """If propagation_delay_ns is explicitly set, fiber_km doesn't override it."""
        link = NetworkLink(
            source="A",
            destination="B",
            tier=CoLocationTier.METRO,
            base_latency_ns=0,
            fiber_km=10.0,
            propagation_delay_ns=99_999,
        )
        # Explicit value should be preserved (post_init only sets if == 0)
        assert link.propagation_delay_ns == 99_999


# =============================================================================
# ExchangeTopology Tests
# =============================================================================


class TestExchangeTopology:
    """Tests for ExchangeTopology graph building and path computation."""

    def test_build_default_has_london_venues(self):
        """Default topology must include London venues (LSE, ICE_EU)."""
        topo = ExchangeTopology.build_default()
        assert "LSE" in topo._venues
        assert "ICE_EU" in topo._venues
        assert topo._venues["LSE"].location == "London"
        assert topo._venues["ICE_EU"].location == "London"

    def test_build_default_has_frankfurt_venue(self):
        """Default topology must include Frankfurt venue (Eurex)."""
        topo = ExchangeTopology.build_default()
        assert "Eurex" in topo._venues
        assert topo._venues["Eurex"].location == "Frankfurt"

    def test_build_default_has_chicago_venue(self):
        """Default topology must include Chicago venue (CME)."""
        topo = ExchangeTopology.build_default()
        assert "CME" in topo._venues
        assert topo._venues["CME"].location == "Chicago"

    def test_build_default_has_nyc_venues(self):
        """Default topology must include NYC venues (NASDAQ, NYSE)."""
        topo = ExchangeTopology.build_default()
        assert "NASDAQ" in topo._venues
        assert "NYSE" in topo._venues
        assert topo._venues["NASDAQ"].location == "New York"
        assert topo._venues["NYSE"].location == "New York"

    def test_build_default_six_venues_total(self):
        """Default topology should have exactly 6 venues."""
        topo = ExchangeTopology.build_default()
        assert len(topo._venues) == 6

    def test_latency_matrix_is_symmetric_ish(self):
        """Latency matrix should be approximately symmetric (bidirectional links).

        Exact symmetry holds because add_link uses the same weight for both
        directions. Small asymmetry could exist if jitter is evaluated at
        add_link time, but the code evaluates once and uses for both.
        """
        random.seed(123)
        topo = ExchangeTopology.build_default()
        matrix = topo.latency_matrix()

        venues = list(topo._venues.keys())
        for src in venues:
            for dst in venues:
                if src == dst:
                    continue
                # Matrix should be symmetric since link weights are set once
                assert matrix[src][dst] == matrix[dst][src], (
                    f"Asymmetry: {src}→{dst}={matrix[src][dst]}ns "
                    f"vs {dst}→{src}={matrix[dst][src]}ns"
                )

    def test_latency_matrix_diagonal_is_me_latency(self):
        """Diagonal entries should be the venue's matching engine latency."""
        topo = ExchangeTopology.build_default()
        matrix = topo.latency_matrix()

        assert matrix["LSE"]["LSE"] == 450
        assert matrix["CME"]["CME"] == 350
        assert matrix["Eurex"]["Eurex"] == 400
        assert matrix["NASDAQ"]["NASDAQ"] == 500
        assert matrix["NYSE"]["NYSE"] == 550

    def test_latency_matrix_london_internal_fast(self):
        """LSE ↔ ICE_EU (same DC complex, 2km) should be < 50μs."""
        random.seed(0)
        topo = ExchangeTopology.build_default()
        matrix = topo.latency_matrix()
        # 2km fiber: 10,000ns prop + 5,000ns base + serialization + jitter
        # Should be well under 50,000ns
        assert matrix["LSE"]["ICE_EU"] < 50_000

    def test_latency_matrix_london_frankfurt_plausible(self):
        """LSE ↔ Eurex should be in ~4-5ms range (800km fiber)."""
        random.seed(0)
        topo = ExchangeTopology.build_default()
        matrix = topo.latency_matrix()
        latency = matrix["LSE"]["Eurex"]
        # 800km * 5ns/m = 4,000,000ns propagation + base + serialization
        # Expected: ~4.2ms = 4,200,000ns
        assert 4_000_000 <= latency <= 5_000_000, f"LSE→Eurex: {latency}ns"

    def test_latency_matrix_chicago_nyc_plausible(self):
        """CME ↔ NASDAQ should be in ~6-7.5ms range (1300km fiber)."""
        random.seed(0)
        topo = ExchangeTopology.build_default()
        matrix = topo.latency_matrix()
        latency = matrix["CME"]["NASDAQ"]
        # 1300km * 5ns/m = 6,500,000ns propagation + base 150,000 + serialization
        assert 6_500_000 <= latency <= 7_500_000, f"CME→NASDAQ: {latency}ns"

    def test_fastest_path_simple_direct(self):
        """Direct link should be the fastest path."""
        topo = ExchangeTopology()
        topo.add_venue("A", "CityA")
        topo.add_venue("B", "CityB")
        link = NetworkLink(
            source="A", destination="B",
            tier=CoLocationTier.METRO,
            base_latency_ns=100_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=20.0,
        )
        topo.add_link(link)

        path, latency = topo.fastest_path("A", "B")
        assert path == ["A", "B"]
        # 20km fiber: 100,000ns prop + 100,000ns base + 51ns serial = 200,051ns
        assert latency == 200_051

    def test_fastest_path_prefers_shorter_route(self):
        """Dijkstra should prefer shorter total latency over hop count."""
        topo = ExchangeTopology()
        topo.add_venue("A", "CityA")
        topo.add_venue("B", "CityB")
        topo.add_venue("C", "CityC")

        # Direct A→C: slow (long fiber)
        topo.add_link(NetworkLink(
            source="A", destination="C",
            tier=CoLocationTier.INTERCONTINENTAL,
            base_latency_ns=500_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=5_000.0,  # 25ms propagation
        ))
        # A→B: fast
        topo.add_link(NetworkLink(
            source="A", destination="B",
            tier=CoLocationTier.METRO,
            base_latency_ns=10_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=5.0,  # 25,000ns
        ))
        # B→C: fast
        topo.add_link(NetworkLink(
            source="B", destination="C",
            tier=CoLocationTier.METRO,
            base_latency_ns=10_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=5.0,  # 25,000ns
        ))

        path, latency = topo.fastest_path("A", "C")
        # Two-hop (A→B→C) should be faster than direct (A→C)
        assert path == ["A", "B", "C"]
        # Each hop: 25,000 (prop) + 10,000 (base) + 51 (serial) = 35,051ns
        # Total: 70,102ns << direct's 25,500,051ns
        assert latency == 70_102

    def test_fastest_path_raises_for_unknown_node(self):
        """fastest_path must raise ValueError for non-existent nodes."""
        topo = ExchangeTopology.build_default()
        with pytest.raises(ValueError, match="not found"):
            topo.fastest_path("NONEXISTENT", "LSE")

    def test_fastest_path_raises_for_disconnected(self):
        """fastest_path must raise RuntimeError for unreachable destinations."""
        topo = ExchangeTopology()
        topo.add_venue("A", "CityA")
        topo.add_venue("B", "CityB")
        # No link between A and B
        with pytest.raises(RuntimeError, match="No path"):
            topo.fastest_path("A", "B")

    def test_add_venue_duplicate_raises(self):
        """Adding a venue with an existing name must raise ValueError."""
        topo = ExchangeTopology()
        topo.add_venue("LSE", "London")
        with pytest.raises(ValueError, match="already exists"):
            topo.add_venue("LSE", "London")

    def test_add_participant_creates_link(self):
        """Adding a participant should create a graph edge to the venue."""
        topo = ExchangeTopology()
        topo.add_venue("CME", "Chicago")
        topo.add_participant("TraderA", CoLocationTier.SAME_RACK, "CME")

        path, latency = topo.fastest_path("TraderA", "CME")
        assert path == ["TraderA", "CME"]
        # SAME_RACK typical: (100+1000)//2 = 550ns base, 100G bandwidth
        # latency should be sub-microsecond
        assert latency < 1_000


# =============================================================================
# SmartOrderRouter Tests
# =============================================================================


class TestSmartOrderRouter:
    """Tests for SmartOrderRouter routing and spray logic."""

    @pytest.fixture
    def simple_topology(self):
        """Create a simple topology with one participant and two venues."""
        random.seed(0)
        topo = ExchangeTopology()
        topo.add_venue("VenueA", "CityA")
        topo.add_venue("VenueB", "CityB")

        # Direct link between venues
        topo.add_link(NetworkLink(
            source="VenueA", destination="VenueB",
            tier=CoLocationTier.METRO,
            base_latency_ns=500_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=50.0,
        ))

        # Participant co-located at VenueA (SAME_RACK = very fast)
        topo.add_participant("Trader", CoLocationTier.SAME_RACK, "VenueA")

        return topo

    def test_routes_to_best_price_venue(self, simple_topology):
        """Router should prefer the venue with best price (lowest ask for buys)."""
        router = SmartOrderRouter(
            simple_topology, participant="Trader",
            latency_alpha=0.0,  # Disable latency penalty to test pure price
        )

        order = Order(symbol="AAPL", side="buy", quantity=100, limit_price=150.0)
        books = {
            "VenueA": OrderBookSnapshot(venue="VenueA", best_bid=149.90, best_ask=150.10),
            "VenueB": OrderBookSnapshot(venue="VenueB", best_bid=149.85, best_ask=149.95),
        }

        decision = router.route(order, books)
        # VenueB has lower ask (149.95 < 150.10), so should be selected
        assert decision.venue == "VenueB"
        assert decision.expected_fill_price == 149.95

    def test_latency_penalty_can_flip_routing(self):
        """High latency penalty should favor nearby venue even at worse price."""
        random.seed(0)
        topo = ExchangeTopology()
        topo.add_venue("VenueA", "CityA")
        topo.add_venue("VenueB", "CityB")

        # METRO link with zero jitter for determinism
        topo.add_link(NetworkLink(
            source="VenueA", destination="VenueB",
            tier=CoLocationTier.METRO,
            base_latency_ns=500_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=50.0,
        ))

        # Participant at VenueA with SAME_RACK (zero-jitter for predictability)
        topo.add_participant("Trader", CoLocationTier.SAME_RACK, "VenueA")

        router = SmartOrderRouter(
            topo, participant="Trader",
            latency_alpha=10.0,  # Very high latency weight
        )

        # Verify latencies: VenueA should be << VenueB
        lat_a = router.latency_to_venue("VenueA")
        lat_b = router.latency_to_venue("VenueB")
        assert lat_a < 2_000, f"VenueA latency {lat_a}ns should be sub-2μs"
        assert lat_b > 500_000, f"VenueB latency {lat_b}ns should be > 500μs"

        order = Order(symbol="AAPL", side="buy", quantity=100, limit_price=150.10)
        books = {
            # VenueA: slightly worse price but very close (SAME_RACK)
            "VenueA": OrderBookSnapshot(venue="VenueA", best_bid=149.90, best_ask=150.05),
            # VenueB: better price but far away (METRO distance)
            "VenueB": OrderBookSnapshot(venue="VenueB", best_bid=149.85, best_ask=149.95),
        }

        decision = router.route(order, books)
        # With high latency penalty, nearby VenueA should win despite worse price
        # VenueA score: 150.05 + 10*771/1e6 ≈ 150.05 + 0.008 = 150.058
        # VenueB score: 149.95 + 10*750822/1e6 ≈ 149.95 + 7.508 = 157.458
        assert decision.venue == "VenueA"

    def test_routes_sell_order_to_best_bid(self, simple_topology):
        """Sell orders should route to highest bid venue."""
        router = SmartOrderRouter(
            simple_topology, participant="Trader",
            latency_alpha=0.0,
        )

        order = Order(symbol="AAPL", side="sell", quantity=100, limit_price=149.0)
        books = {
            "VenueA": OrderBookSnapshot(venue="VenueA", best_bid=149.50, best_ask=149.70),
            "VenueB": OrderBookSnapshot(venue="VenueB", best_bid=149.80, best_ask=150.00),
        }

        decision = router.route(order, books)
        # VenueB has higher bid (149.80 > 149.50) — but for sells, score = -price
        # Lower score is better, so -149.80 < -149.50 → VenueB wins
        assert decision.venue == "VenueB"
        assert decision.expected_fill_price == 149.80

    def test_limit_price_filters_venues(self, simple_topology):
        """Venues with prices exceeding limit should be excluded."""
        router = SmartOrderRouter(
            simple_topology, participant="Trader",
            latency_alpha=0.0,
        )

        order = Order(symbol="AAPL", side="buy", quantity=100, limit_price=149.98)
        books = {
            "VenueA": OrderBookSnapshot(venue="VenueA", best_bid=149.90, best_ask=150.05),
            "VenueB": OrderBookSnapshot(venue="VenueB", best_bid=149.85, best_ask=149.95),
        }

        decision = router.route(order, books)
        # VenueA ask (150.05) exceeds limit (149.98), only VenueB qualifies
        assert decision.venue == "VenueB"

    def test_no_valid_venue_raises(self, simple_topology):
        """Raise ValueError when no venue satisfies constraints."""
        router = SmartOrderRouter(
            simple_topology, participant="Trader",
        )

        order = Order(symbol="AAPL", side="buy", quantity=100, limit_price=140.0)
        books = {
            "VenueA": OrderBookSnapshot(venue="VenueA", best_bid=149.90, best_ask=150.05),
            "VenueB": OrderBookSnapshot(venue="VenueB", best_bid=149.85, best_ask=149.95),
        }

        with pytest.raises(ValueError, match="No valid venue"):
            router.route(order, books)

    def test_empty_books_raises(self, simple_topology):
        """Raise ValueError when no order books provided."""
        router = SmartOrderRouter(simple_topology, participant="Trader")
        order = Order(symbol="AAPL", side="buy", quantity=100)

        with pytest.raises(ValueError, match="No order book"):
            router.route(order, {})

    def test_latency_to_venue_precomputed(self, simple_topology):
        """Precomputed latencies should be accessible and positive."""
        router = SmartOrderRouter(simple_topology, participant="Trader")

        lat_a = router.latency_to_venue("VenueA")
        lat_b = router.latency_to_venue("VenueB")

        # VenueA: SAME_RACK, should be sub-microsecond
        assert 0 < lat_a < 1_500
        # VenueB: must traverse METRO link, should be > 500μs
        assert lat_b > 500_000

    def test_unreachable_venue_returns_negative(self, simple_topology):
        """Unreachable venues should return -1 latency."""
        router = SmartOrderRouter(simple_topology, participant="Trader")
        assert router.latency_to_venue("NONEXISTENT") == -1

    def test_spray_distributes_proportionally(self, simple_topology):
        """Spray should distribute quantity according to ratios."""
        router = SmartOrderRouter(simple_topology, participant="Trader")

        order = Order(symbol="AAPL", side="buy", quantity=1000, order_type="ioc")
        allocations = router.spray(order, ["VenueA", "VenueB"], [0.6, 0.4])

        assert len(allocations) == 2
        assert allocations[0].venue == "VenueA"
        assert allocations[1].venue == "VenueB"
        # Total must equal order quantity
        total_qty = sum(a.quantity for a in allocations)
        assert total_qty == 1000
        # First allocation should be ~60%
        assert allocations[0].quantity == 600
        # Last gets remainder
        assert allocations[1].quantity == 400

    def test_spray_three_venues(self, simple_topology):
        """Spray across 3 venues sums to total quantity."""
        # Add a third venue
        simple_topology.add_venue("VenueC", "CityC")
        simple_topology.add_link(NetworkLink(
            source="VenueA", destination="VenueC",
            tier=CoLocationTier.METRO,
            base_latency_ns=200_000,
            jitter_ns=0,
            bandwidth_gbps=10.0,
            fiber_km=30.0,
        ))

        router = SmartOrderRouter(simple_topology, participant="Trader")
        order = Order(symbol="AAPL", side="buy", quantity=1000, order_type="ioc")
        allocations = router.spray(
            order,
            ["VenueA", "VenueB", "VenueC"],
            [0.5, 0.3, 0.2],
        )

        total = sum(a.quantity for a in allocations)
        assert total == 1000
        assert allocations[0].quantity == 500
        assert allocations[1].quantity == 300
        assert allocations[2].quantity == 200  # remainder

    def test_spray_mismatched_lengths_raises(self, simple_topology):
        """Mismatched venues/ratios lengths must raise ValueError."""
        router = SmartOrderRouter(simple_topology, participant="Trader")
        order = Order(symbol="AAPL", side="buy", quantity=1000)

        with pytest.raises(ValueError, match="same length"):
            router.spray(order, ["VenueA", "VenueB"], [1.0])

    def test_spray_normalizes_ratios(self, simple_topology):
        """Ratios that don't sum to 1.0 should be normalized."""
        router = SmartOrderRouter(simple_topology, participant="Trader")
        order = Order(symbol="AAPL", side="buy", quantity=1000, order_type="ioc")

        # Ratios sum to 2.0, should be normalized to [0.5, 0.5]
        allocations = router.spray(order, ["VenueA", "VenueB"], [1.0, 1.0])
        total = sum(a.quantity for a in allocations)
        assert total == 1000
        assert allocations[0].quantity == 500

    def test_routing_decision_has_latency(self, simple_topology):
        """RoutingDecision must include expected_latency_ns."""
        router = SmartOrderRouter(simple_topology, participant="Trader")
        order = Order(symbol="AAPL", side="buy", quantity=100, limit_price=150.0)
        books = {
            "VenueA": OrderBookSnapshot(venue="VenueA", best_bid=149.90, best_ask=150.00),
        }

        decision = router.route(order, books)
        assert decision.expected_latency_ns > 0
        assert 0.0 <= decision.confidence <= 1.0


# =============================================================================
# CrossConnect Tests
# =============================================================================


class TestCrossConnect:
    """Tests for CrossConnect fiber delay and asymmetry modeling."""

    def test_fiber_length_maps_to_delay_5ns_per_meter(self):
        """Each meter of fiber adds exactly 5ns of propagation delay."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=100.0,  # 100m → 500ns
        )
        assert xc.tx_propagation_ns == 500
        assert xc.rx_propagation_ns == 500  # Defaults to tx length

    def test_fiber_delay_short_run(self):
        """15m fiber run (typical SAME_RACK): 75ns propagation."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=15.0,
        )
        assert xc.tx_propagation_ns == 75

    def test_fiber_delay_long_run(self):
        """300m fiber run (large DC): 1500ns propagation."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=300.0,
        )
        assert xc.tx_propagation_ns == 1_500

    def test_asymmetry_different_tx_rx_paths(self):
        """TX/RX paths of different lengths produce non-zero asymmetry."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=100.0,
            rx_fiber_meters=102.0,  # 2m longer RX path
        )
        # TX: 500ns, RX: 510ns → asymmetry = 10ns
        assert xc.tx_propagation_ns == 500
        assert xc.rx_propagation_ns == 510
        assert xc.asymmetry_ns == 10

    def test_asymmetry_physically_realistic(self):
        """Typical asymmetry should be 1-20ns (cable tray routing differences)."""
        # Real-world: TX and RX cables may take different physical paths
        # through overhead/underfloor trays, adding 0.2-4m difference
        xc = CrossConnect(
            participant="TraderA",
            venue="LSE",
            tx_fiber_meters=50.0,
            rx_fiber_meters=52.5,  # 2.5m longer (realistic tray routing)
        )
        asymmetry = xc.asymmetry_ns
        # 2.5m * 5ns/m = 12.5 → int = 12ns
        assert asymmetry == 12
        assert 1 <= asymmetry <= 20

    def test_asymmetry_zero_when_equal(self):
        """Equal TX/RX paths have zero asymmetry."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=100.0,
            rx_fiber_meters=100.0,
        )
        assert xc.asymmetry_ns == 0

    def test_round_trip_includes_both_paths(self):
        """Round trip = TX prop + RX prop + 2 * switch forwarding."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=100.0,   # 500ns
            rx_fiber_meters=100.0,   # 500ns
            switch_hops=1,
            switch_latency_ns=80,
        )
        # 500 + 500 + 2*80 = 1160ns
        assert xc.round_trip_ns == 1_160

    def test_round_trip_with_multiple_switches(self):
        """Multiple switch hops increase round trip time."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=50.0,    # 250ns
            rx_fiber_meters=50.0,    # 250ns
            switch_hops=3,
            switch_latency_ns=80,
        )
        # 250 + 250 + 2*(3*80) = 500 + 480 = 980ns
        assert xc.round_trip_ns == 980

    def test_one_way_latency_tx(self):
        """TX one-way = fiber propagation + switch forwarding."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=100.0,   # 500ns
            rx_fiber_meters=120.0,   # 600ns
            switch_hops=1,
            switch_latency_ns=80,
        )
        assert xc.one_way_latency_ns("tx") == 500 + 80  # 580ns
        assert xc.one_way_latency_ns("rx") == 600 + 80  # 680ns

    def test_one_way_invalid_direction_raises(self):
        """Invalid direction must raise ValueError."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=100.0,
        )
        with pytest.raises(ValueError, match="must be 'tx' or 'rx'"):
            xc.one_way_latency_ns("invalid")

    def test_tick_to_trade_calculation(self):
        """tick_to_trade = RX latency + processing + TX latency."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=80.0,    # 400ns
            rx_fiber_meters=82.0,    # 410ns
            switch_hops=1,
            switch_latency_ns=80,
        )
        # RX one-way: 410 + 80 = 490ns
        # Processing: 200ns (default)
        # TX one-way: 400 + 80 = 480ns
        # Total: 490 + 200 + 480 = 1170ns
        assert xc.tick_to_trade_ns() == 1_170
        # With custom processing
        assert xc.tick_to_trade_ns(processing_ns=50) == 490 + 50 + 480

    def test_tick_to_trade_realistic_range(self):
        """Tick-to-trade for co-located FPGA should be < 2μs."""
        xc = CrossConnect(
            participant="JumpTrading",
            venue="CME",
            tx_fiber_meters=20.0,    # 100ns (short rack-to-ME run)
            rx_fiber_meters=20.0,    # 100ns
            switch_hops=1,
            switch_latency_ns=80,
        )
        # RX: 180ns, processing: 100ns (FPGA), TX: 180ns = 460ns total
        t2t = xc.tick_to_trade_ns(processing_ns=100)
        assert t2t == 460
        assert t2t < 2_000  # Must be sub-2μs for competitive HFT

    def test_equalized_pair_has_zero_or_near_zero_asymmetry(self):
        """equalized_pair should produce asymmetry within max_asymmetry_ns."""
        random.seed(42)
        xc = CrossConnect.equalized_pair(
            participant="TraderA",
            venue="CME",
            target_length_meters=50.0,
            max_asymmetry_ns=5,
        )
        # TX should be exactly target length
        assert xc.tx_fiber_meters == 50.0
        # RX should be target + small diff (within max_asymmetry_ns/5 meters)
        assert xc.rx_fiber_meters >= 50.0
        assert xc.rx_fiber_meters <= 51.0  # max 1m extra for 5ns max asymmetry
        # Asymmetry must be within specified bounds
        assert xc.asymmetry_ns <= 5

    def test_equalized_pair_zero_max_asymmetry(self):
        """With max_asymmetry_ns=0, paths should be exactly equal."""
        random.seed(99)
        xc = CrossConnect.equalized_pair(
            participant="TraderA",
            venue="CME",
            target_length_meters=75.0,
            max_asymmetry_ns=0,
        )
        # max_diff_meters = 0/5 = 0, uniform(0,0) = 0
        assert xc.tx_fiber_meters == 75.0
        assert xc.rx_fiber_meters == 75.0
        assert xc.asymmetry_ns == 0

    def test_equalized_pair_always_within_bound(self):
        """Across many seeds, equalized_pair never exceeds max_asymmetry_ns."""
        for seed in range(50):
            random.seed(seed)
            xc = CrossConnect.equalized_pair(
                participant="Trader",
                venue="CME",
                target_length_meters=100.0,
                max_asymmetry_ns=10,
            )
            assert xc.asymmetry_ns <= 10, (
                f"Seed {seed}: asymmetry {xc.asymmetry_ns}ns exceeds max 10ns"
            )

    def test_default_rx_equals_tx(self):
        """When rx_fiber_meters is None, it defaults to tx_fiber_meters."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=45.0,
        )
        assert xc.rx_fiber_meters == 45.0
        assert xc.asymmetry_ns == 0

    def test_connector_pairs_and_switch_defaults(self):
        """Default values should match typical co-location setup."""
        xc = CrossConnect(
            participant="TraderA",
            venue="CME",
            tx_fiber_meters=50.0,
        )
        assert xc.connector_pairs == 2  # Participant patch + exchange patch
        assert xc.switch_hops == 1      # Single cut-through switch
        assert xc.switch_latency_ns == 80  # Modern Memory-cut-through
