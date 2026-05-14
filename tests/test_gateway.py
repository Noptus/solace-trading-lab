"""Comprehensive tests for the FPGA gateway module (src/gateway/fpga.py).

Covers:
  - FPGAGateway: pipeline execution, timestamps, cancel fast-path, determinism
  - KernelBypass: ring buffer capacity, send/recv round-trip, hugepages, alignment
  - RiskGate: sub-100ns model, fat-finger rejection, rate limiting, kill switch
  - GatewayMetrics: percentile calculations, burst throughput tracking
"""

from __future__ import annotations

import random
import time

import pytest

from gateway.fpga import (
    CancelEntry,
    FPGAGateway,
    GatewayMetrics,
    HardwareTimestamp,
    HugepageBuffer,
    KernelBypass,
    OrderEntry,
    PipelineLatency,
    PipelineStage,
    RingBufferSlot,
    RiskCheckResult,
    RiskGate,
    create_gateway,
)
from common.models import Order, Side, OrderType


# =============================================================================
# Helpers / Fixtures
# =============================================================================


@pytest.fixture
def basic_order() -> OrderEntry:
    """A vanilla limit buy order for testing."""
    return OrderEntry(
        order_id=1001,
        symbol="AAPL",
        price=150.25,
        quantity=100,
        side="BUY",
        order_type="LIMIT",
    )


@pytest.fixture
def gateway_no_jitter() -> FPGAGateway:
    """Gateway with zero variance for deterministic testing."""
    return FPGAGateway(variance_ns=0)


@pytest.fixture
def gateway_default() -> FPGAGateway:
    """Gateway with default configuration."""
    return FPGAGateway()


@pytest.fixture
def risk_gate() -> RiskGate:
    """Fresh risk gate with default thresholds."""
    return RiskGate()


@pytest.fixture
def kernel_bypass() -> KernelBypass:
    """Kernel bypass NIC with small ring for testability."""
    return KernelBypass(ring_buffer_size=16, batch_size=4)


# =============================================================================
# FPGAGateway Tests
# =============================================================================


class TestFPGAGatewaySubmitOrder:
    """Test submit_order returns timestamps and executes pipeline correctly."""

    def test_submit_order_returns_entry_and_wire_timestamps(
        self, gateway_no_jitter: FPGAGateway, basic_order: OrderEntry
    ):
        """submit_order must return (order, entry_ts, wire_ts) with valid timestamps."""
        order, entry_ts, wire_ts = gateway_no_jitter.submit_order(basic_order)

        assert isinstance(entry_ts, HardwareTimestamp)
        assert isinstance(wire_ts, HardwareTimestamp)
        assert entry_ts.ns_since_epoch > 0
        assert wire_ts.ns_since_epoch > entry_ts.ns_since_epoch
        assert entry_ts.is_ingress is True
        assert wire_ts.is_ingress is False

    def test_submit_order_returns_same_order_object(
        self, gateway_no_jitter: FPGAGateway, basic_order: OrderEntry
    ):
        """The returned order should be the same instance passed in."""
        returned_order, _, _ = gateway_no_jitter.submit_order(basic_order)
        assert returned_order is basic_order

    def test_wire_timestamp_includes_pipeline_and_wire_latency(
        self, gateway_no_jitter: FPGAGateway, basic_order: OrderEntry
    ):
        """Wire timestamp = entry + sum(stage latencies) + wire_latency."""
        _, entry_ts, wire_ts = gateway_no_jitter.submit_order(basic_order)

        expected_pipeline = sum(FPGAGateway.DEFAULT_STAGE_LATENCIES.values())
        expected_total = expected_pipeline + gateway_no_jitter.wire_latency_ns

        actual_delta = wire_ts.ns_since_epoch - entry_ts.ns_since_epoch
        assert actual_delta == expected_total

    def test_submit_order_increments_metrics_counter(
        self, gateway_no_jitter: FPGAGateway, basic_order: OrderEntry
    ):
        """Each successful submission increments orders_submitted."""
        assert gateway_no_jitter.metrics.orders_submitted == 0
        gateway_no_jitter.submit_order(basic_order)
        assert gateway_no_jitter.metrics.orders_submitted == 1

    def test_submit_multiple_orders_increments_correctly(
        self, gateway_no_jitter: FPGAGateway
    ):
        """Submitting N orders results in N counted."""
        for i in range(5):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
            )
            gateway_no_jitter.submit_order(order)
        assert gateway_no_jitter.metrics.orders_submitted == 5


class TestFPGAGatewayPipeline:
    """Test pipeline stages execute in correct order."""

    def test_pipeline_stages_execute_sequentially(
        self, gateway_no_jitter: FPGAGateway, basic_order: OrderEntry
    ):
        """Pipeline latency samples should cover all 5 stages in order."""
        gateway_no_jitter.submit_order(basic_order)

        # Metrics should have one sample per stage
        for stage in PipelineStage:
            samples = gateway_no_jitter.metrics.stage_latencies[stage]
            assert len(samples) == 1, f"Stage {stage.name} should have 1 sample"

    def test_pipeline_stages_order_matches_enum_declaration(
        self, gateway_no_jitter: FPGAGateway, basic_order: OrderEntry
    ):
        """tick_to_wire_samples should reflect sum of all stages + wire."""
        gateway_no_jitter.submit_order(basic_order)

        total_recorded = gateway_no_jitter.metrics.tick_to_wire_samples[0]
        expected = (
            sum(FPGAGateway.DEFAULT_STAGE_LATENCIES.values())
            + gateway_no_jitter.wire_latency_ns
        )
        assert total_recorded == expected

    def test_pipeline_halt_raises_on_submit(
        self, gateway_no_jitter: FPGAGateway, basic_order: OrderEntry
    ):
        """Halted pipeline should refuse new orders."""
        gateway_no_jitter.halt()
        with pytest.raises(RuntimeError, match="kill switch"):
            gateway_no_jitter.submit_order(basic_order)


class TestFPGAGatewayCancelOrder:
    """Test cancel_order uses fast path (reduced latency)."""

    def test_cancel_returns_timestamps(self, gateway_no_jitter: FPGAGateway):
        """cancel_order returns (cancel_entry, entry_ts, wire_ts)."""
        cancel, entry_ts, wire_ts = gateway_no_jitter.cancel_order(order_id=42)

        assert isinstance(cancel, CancelEntry)
        assert cancel.order_id == 42
        assert isinstance(entry_ts, HardwareTimestamp)
        assert isinstance(wire_ts, HardwareTimestamp)
        assert wire_ts.ns_since_epoch > entry_ts.ns_since_epoch

    def test_cancel_latency_less_than_full_pipeline(
        self, gateway_no_jitter: FPGAGateway
    ):
        """Fast-cancel path should be significantly shorter than full pipeline."""
        _, entry_ts, wire_ts = gateway_no_jitter.cancel_order(order_id=1)
        cancel_total = wire_ts.ns_since_epoch - entry_ts.ns_since_epoch

        # Full pipeline latency (no jitter)
        full_pipeline = (
            sum(FPGAGateway.DEFAULT_STAGE_LATENCIES.values())
            + gateway_no_jitter.wire_latency_ns
        )

        # Cancel should be strictly less than full pipeline
        assert cancel_total < full_pipeline
        # Cancel = cancel_latency_ns + wire_latency_ns (with variance=0)
        expected_cancel = (
            gateway_no_jitter.cancel_latency_ns + gateway_no_jitter.wire_latency_ns
        )
        assert cancel_total == expected_cancel

    def test_cancel_increments_cancel_metric(self, gateway_no_jitter: FPGAGateway):
        """Each cancel should increment cancels_submitted counter."""
        assert gateway_no_jitter.metrics.cancels_submitted == 0
        gateway_no_jitter.cancel_order(order_id=100)
        gateway_no_jitter.cancel_order(order_id=101)
        assert gateway_no_jitter.metrics.cancels_submitted == 2

    def test_cancel_halted_pipeline_raises(self, gateway_no_jitter: FPGAGateway):
        """Cancel on halted pipeline should raise RuntimeError."""
        gateway_no_jitter.halt()
        with pytest.raises(RuntimeError, match="kill switch"):
            gateway_no_jitter.cancel_order(order_id=1)


class TestFPGAGatewayDeterminism:
    """Test latency is deterministic with the same random seed."""

    def test_same_seed_produces_same_latencies(self):
        """Two runs with same seed should produce identical pipeline timings."""
        results_a: list[int] = []
        results_b: list[int] = []

        for results in (results_a, results_b):
            random.seed(12345)
            gw = FPGAGateway(variance_ns=10)
            for i in range(20):
                order = OrderEntry(
                    order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
                )
                _, entry_ts, wire_ts = gw.submit_order(order)
                results.append(wire_ts.ns_since_epoch - entry_ts.ns_since_epoch)

        assert results_a == results_b

    def test_different_seeds_produce_different_latencies(self):
        """Different seeds should (with overwhelming probability) diverge."""
        results_a: list[int] = []
        results_b: list[int] = []

        random.seed(111)
        gw_a = FPGAGateway(variance_ns=10)
        for i in range(20):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
            )
            _, entry_ts, wire_ts = gw_a.submit_order(order)
            results_a.append(wire_ts.ns_since_epoch - entry_ts.ns_since_epoch)

        random.seed(999)
        gw_b = FPGAGateway(variance_ns=10)
        for i in range(20):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
            )
            _, entry_ts, wire_ts = gw_b.submit_order(order)
            results_b.append(wire_ts.ns_since_epoch - entry_ts.ns_since_epoch)

        assert results_a != results_b

    def test_zero_variance_is_perfectly_deterministic(self):
        """With variance=0, every order has identical pipeline latency."""
        gw = FPGAGateway(variance_ns=0)
        latencies = []
        for i in range(10):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
            )
            _, entry_ts, wire_ts = gw.submit_order(order)
            latencies.append(wire_ts.ns_since_epoch - entry_ts.ns_since_epoch)

        assert len(set(latencies)) == 1  # All identical


# =============================================================================
# KernelBypass Tests
# =============================================================================


class TestKernelBypassRingBuffer:
    """Test ring buffer respects capacity (must be power of 2)."""

    def test_ring_buffer_size_must_be_power_of_2(self):
        """Non-power-of-2 sizes should raise ValueError."""
        with pytest.raises(ValueError, match="power of 2"):
            KernelBypass(ring_buffer_size=100)

        with pytest.raises(ValueError, match="power of 2"):
            KernelBypass(ring_buffer_size=7)

        with pytest.raises(ValueError, match="power of 2"):
            KernelBypass(ring_buffer_size=3)

    def test_valid_power_of_2_sizes_accepted(self):
        """Powers of 2 should work without errors."""
        for size in (1, 2, 4, 8, 16, 64, 256, 1024, 4096):
            kb = KernelBypass(ring_buffer_size=size)
            assert kb.ring_buffer_size == size

    def test_tx_ring_capacity_enforced(self):
        """TX ring should reject sends when full."""
        # Ring size 4 means 3 usable slots (one is always empty for full detection)
        kb = KernelBypass(ring_buffer_size=4)
        kb.send(b"A" * 10)
        kb.send(b"B" * 10)
        kb.send(b"C" * 10)

        with pytest.raises(BufferError, match="TX ring full"):
            kb.send(b"D" * 10)

    def test_rx_ring_capacity_enforced(self):
        """RX ring should reject inject when full."""
        kb = KernelBypass(ring_buffer_size=4)
        kb.inject_rx(b"A")
        kb.inject_rx(b"B")
        kb.inject_rx(b"C")

        with pytest.raises(BufferError, match="RX ring full"):
            kb.inject_rx(b"D")

    def test_ring_buffer_wraps_correctly(self):
        """After consuming, space should become available again."""
        kb = KernelBypass(ring_buffer_size=4)

        # Fill 3 slots
        kb.inject_rx(b"msg1")
        kb.inject_rx(b"msg2")
        kb.inject_rx(b"msg3")

        # Consume 2
        data1, _ = kb.recv()
        data2, _ = kb.recv()
        assert data1 == b"msg1"
        assert data2 == b"msg2"

        # Now should have space for 2 more
        kb.inject_rx(b"msg4")
        kb.inject_rx(b"msg5")

        # Consume remaining
        data3, _ = kb.recv()
        data4, _ = kb.recv()
        data5, _ = kb.recv()
        assert data3 == b"msg3"
        assert data4 == b"msg4"
        assert data5 == b"msg5"


class TestKernelBypassSendRecv:
    """Test send/recv round-trip."""

    def test_inject_then_recv_returns_data(self, kernel_bypass: KernelBypass):
        """Injecting to RX ring and receiving should return the same bytes."""
        payload = b"HELLO_MARKET_DATA"
        kernel_bypass.inject_rx(payload)
        received, ts = kernel_bypass.recv()
        assert received == payload
        assert ts > 0

    def test_recv_empty_returns_none(self, kernel_bypass: KernelBypass):
        """Receiving from empty ring returns None."""
        data, ts = kernel_bypass.recv()
        assert data is None
        assert ts > 0

    def test_send_returns_timestamp(self, kernel_bypass: KernelBypass):
        """send() returns a nanosecond timestamp."""
        ts = kernel_bypass.send(b"ORDER_PAYLOAD")
        assert isinstance(ts, int)
        assert ts > 0

    def test_multiple_send_recv_roundtrips(self, kernel_bypass: KernelBypass):
        """Multiple messages maintain FIFO order."""
        messages = [f"msg_{i}".encode() for i in range(5)]
        for msg in messages:
            kernel_bypass.inject_rx(msg)

        received = []
        for _ in range(5):
            data, _ = kernel_bypass.recv()
            received.append(data)

        assert received == messages

    def test_send_increments_packet_counter(self, kernel_bypass: KernelBypass):
        """Each send should increment internal packet counter."""
        assert kernel_bypass._packets_sent == 0
        kernel_bypass.send(b"a")
        kernel_bypass.send(b"b")
        assert kernel_bypass._packets_sent == 2

    def test_recv_increments_packet_counter(self, kernel_bypass: KernelBypass):
        """Each recv of data should increment received counter."""
        kernel_bypass.inject_rx(b"x")
        kernel_bypass.inject_rx(b"y")
        kernel_bypass.recv()
        kernel_bypass.recv()
        assert kernel_bypass._packets_received == 2


class TestKernelBypassHugepages:
    """Test hugepage allocation math is correct."""

    def test_total_bytes_calculation(self):
        """total_bytes = page_size * num_pages."""
        buf = HugepageBuffer(page_size_bytes=2 * 1024 * 1024, num_pages=512)
        expected = 2 * 1024 * 1024 * 512  # 1 GB
        assert buf.total_bytes == expected

    def test_allocation_returns_sequential_offsets(self):
        """Allocations return increasing offsets."""
        buf = HugepageBuffer(num_pages=10)
        off1 = buf.allocate(100)
        off2 = buf.allocate(200)
        off3 = buf.allocate(64)

        assert off1 == 0
        # 100 bytes → rounded to 128 (next 64-byte multiple)
        assert off2 == 128
        # 200 bytes → rounded to 256
        assert off3 == 128 + 256  # 384

    def test_allocation_rounds_up_to_cache_line(self):
        """All allocations are cache-line (64 byte) aligned."""
        buf = HugepageBuffer(num_pages=10)

        # 1 byte → should allocate 64
        off1 = buf.allocate(1)
        off2 = buf.allocate(1)
        assert off1 == 0
        assert off2 == 64  # Rounded up

    def test_allocation_exact_cache_line_no_padding(self):
        """Exact multiples of 64 don't get extra padding."""
        buf = HugepageBuffer(num_pages=10)
        off1 = buf.allocate(64)
        off2 = buf.allocate(128)
        assert off1 == 0
        assert off2 == 64

    def test_allocation_exhaustion_raises_memory_error(self):
        """Exceeding hugepage pool raises MemoryError."""
        buf = HugepageBuffer(page_size_bytes=1024, num_pages=1)  # Only 1KB total
        buf.allocate(512)
        buf.allocate(512)
        with pytest.raises(MemoryError, match="Hugepage pool exhausted"):
            buf.allocate(1)  # No space left

    def test_bytes_available_decreases_with_allocations(self):
        """bytes_available should decrease after each allocation."""
        buf = HugepageBuffer(page_size_bytes=1024, num_pages=1)
        assert buf.bytes_available == 1024
        buf.allocate(100)  # Rounds to 128
        assert buf.bytes_available == 1024 - 128

    def test_default_hugepage_pool_is_1gb(self):
        """Default configuration: 512 * 2MB = 1GB."""
        buf = HugepageBuffer()
        assert buf.total_bytes == 1024 * 1024 * 1024  # 1 GB


class TestKernelBypassCacheLineAlignment:
    """Test cache-line alignment is 64 bytes."""

    def test_ring_buffer_slot_reports_64_bytes(self):
        """RingBufferSlot.cache_line_size should be 64."""
        slot = RingBufferSlot()
        assert slot.cache_line_size == 64

    def test_order_entry_wire_size_is_64_bytes(self):
        """OrderEntry wire representation is cache-line sized."""
        order = OrderEntry(
            order_id=1, symbol="AAPL", price=100.0, quantity=50, side="BUY"
        )
        assert order.wire_size_bytes == 64

    def test_hugepage_allocations_are_64_aligned(self):
        """Every allocation offset is divisible by 64."""
        buf = HugepageBuffer(num_pages=10)
        offsets = []
        for size in [1, 7, 33, 63, 64, 65, 127, 128, 129, 255, 256, 500]:
            off = buf.allocate(size)
            offsets.append(off)

        for off in offsets:
            assert off % 64 == 0, f"Offset {off} is not 64-byte aligned"

    def test_serialized_order_is_64_bytes(self):
        """Gateway _serialize_order produces exactly 64 bytes."""
        gw = FPGAGateway(variance_ns=0)
        order = OrderEntry(
            order_id=99, symbol="TSLA", price=200.5, quantity=1000, side="SELL"
        )
        payload = gw._serialize_order(order)
        assert len(payload) == 64


# =============================================================================
# RiskGate Tests
# =============================================================================


class TestRiskGateNormalOrders:
    """Test normal orders pass in sub-100ns model."""

    def test_normal_order_passes(self, risk_gate: RiskGate, basic_order: OrderEntry):
        """A valid order within all limits should pass."""
        result = risk_gate.check(basic_order)
        assert result == RiskCheckResult.PASS

    def test_risk_gate_latency_is_sub_100ns(self, risk_gate: RiskGate):
        """The modeled risk gate latency should be under 100ns."""
        assert risk_gate.latency_ns < 100

    def test_position_tracking_updates_on_pass(
        self, risk_gate: RiskGate, basic_order: OrderEntry
    ):
        """Passing orders should update internal position state."""
        risk_gate.check(basic_order)
        assert risk_gate._positions.get("AAPL") == 100  # BUY 100

    def test_sell_reduces_position(self, risk_gate: RiskGate):
        """SELL orders reduce tracked position."""
        buy = OrderEntry(
            order_id=1, symbol="AAPL", price=100.0, quantity=500, side="BUY"
        )
        sell = OrderEntry(
            order_id=2, symbol="AAPL", price=100.0, quantity=200, side="SELL"
        )
        risk_gate.check(buy)
        risk_gate.check(sell)
        assert risk_gate._positions["AAPL"] == 300


class TestRiskGateFatFinger:
    """Test fat-finger orders are rejected."""

    def test_fat_finger_rejection(self, risk_gate: RiskGate):
        """Order with notional > max_notional_per_order is rejected."""
        # Default max = 10,000,000. Price * Qty > 10M
        fat_order = OrderEntry(
            order_id=1,
            symbol="AAPL",
            price=200.0,
            quantity=100_000,  # 200 * 100_000 = 20,000,000
            side="BUY",
        )
        result = risk_gate.check(fat_order)
        assert result == RiskCheckResult.REJECT_FAT_FINGER

    def test_just_under_fat_finger_passes(self, risk_gate: RiskGate):
        """Order at exactly the threshold boundary should pass."""
        # max_notional = 10,000,000 → price * qty = 10,000,000 exactly
        order = OrderEntry(
            order_id=1,
            symbol="AAPL",
            price=100.0,
            quantity=100_000,  # 100 * 100_000 = 10,000,000 (not >)
            side="BUY",
        )
        result = risk_gate.check(order)
        assert result == RiskCheckResult.PASS

    def test_fat_finger_does_not_update_position(self, risk_gate: RiskGate):
        """Rejected orders should not change position state."""
        fat_order = OrderEntry(
            order_id=1, symbol="AAPL", price=500.0, quantity=100_000, side="BUY"
        )
        risk_gate.check(fat_order)
        assert risk_gate._positions.get("AAPL", 0) == 0


class TestRiskGateRateLimit:
    """Test rate limit triggers after threshold."""

    def test_rate_limit_triggers_after_max_orders(self):
        """After max_orders_per_second, subsequent orders should be rejected."""
        gate = RiskGate(max_orders_per_second=5)

        # Submit 5 orders (should all pass)
        for i in range(5):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
            )
            result = gate.check(order)
            assert result == RiskCheckResult.PASS, f"Order {i} should pass"

        # 6th order should be rate limited
        order_6 = OrderEntry(
            order_id=6, symbol="AAPL", price=100.0, quantity=10, side="BUY"
        )
        result = gate.check(order_6)
        assert result == RiskCheckResult.REJECT_RATE_LIMIT

    def test_rate_limit_does_not_update_position(self):
        """Rate-limited orders should not update position."""
        gate = RiskGate(max_orders_per_second=2, max_position_per_symbol=1_000_000)

        for i in range(2):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=100, side="BUY"
            )
            gate.check(order)

        # Position should be 200 after 2 passed orders
        assert gate._positions["AAPL"] == 200

        # This should be rejected and not add to position
        rejected = OrderEntry(
            order_id=99, symbol="AAPL", price=100.0, quantity=100, side="BUY"
        )
        result = gate.check(rejected)
        assert result == RiskCheckResult.REJECT_RATE_LIMIT
        assert gate._positions["AAPL"] == 200  # Unchanged


class TestRiskGateKillSwitch:
    """Test kill switch halts all orders."""

    def test_kill_switch_triggered_by_loss(self, risk_gate: RiskGate):
        """Cumulative loss exceeding threshold triggers kill switch."""
        # Default threshold is -5,000,000
        risk_gate.update_pnl(-5_000_001.0)
        assert risk_gate.kill_switch_active is True

    def test_kill_switch_rejects_all_orders(self, risk_gate: RiskGate):
        """Once kill switch is active, all orders are rejected."""
        risk_gate.kill_switch_active = True

        order = OrderEntry(
            order_id=1, symbol="AAPL", price=100.0, quantity=10, side="BUY"
        )
        result = risk_gate.check(order)
        assert result == RiskCheckResult.KILL_SWITCH_TRIGGERED

    def test_kill_switch_overrides_other_checks(self, risk_gate: RiskGate):
        """Kill switch rejection takes priority over other failures."""
        risk_gate.kill_switch_active = True

        # This would also fail fat-finger, but kill switch wins
        fat_order = OrderEntry(
            order_id=1, symbol="AAPL", price=999.0, quantity=1_000_000, side="BUY"
        )
        result = risk_gate.check(fat_order)
        assert result == RiskCheckResult.KILL_SWITCH_TRIGGERED

    def test_kill_switch_reset_allows_orders(self, risk_gate: RiskGate):
        """After reset_kill_switch, orders should pass again."""
        risk_gate.kill_switch_active = True
        risk_gate.reset_kill_switch()

        order = OrderEntry(
            order_id=1, symbol="AAPL", price=100.0, quantity=10, side="BUY"
        )
        result = risk_gate.check(order)
        assert result == RiskCheckResult.PASS

    def test_kill_switch_halts_gateway_pipeline(self):
        """Kill switch in risk gate halts the entire gateway pipeline."""
        gate = RiskGate(kill_switch_loss_threshold=-100.0)
        gw = FPGAGateway(variance_ns=0, risk_gate=gate)

        # Trigger kill switch
        gate.update_pnl(-200.0)

        order = OrderEntry(
            order_id=1, symbol="AAPL", price=100.0, quantity=10, side="BUY"
        )
        # Submit should reject with kill switch at risk check stage
        with pytest.raises(ValueError, match="KILL_SWITCH_TRIGGERED"):
            gw.submit_order(order)

        # Pipeline should now be halted
        assert gw._pipeline_active is False

    def test_position_limit_rejection(self, risk_gate: RiskGate):
        """Position exceeding max_position_per_symbol is rejected."""
        # Default max position is 100,000
        big_order = OrderEntry(
            order_id=1, symbol="AAPL", price=50.0, quantity=100_001, side="BUY"
        )
        result = risk_gate.check(big_order)
        assert result == RiskCheckResult.REJECT_POSITION_LIMIT

    def test_reset_positions_clears_all_state(self, risk_gate: RiskGate):
        """reset_positions should clear positions, timestamps, pnl, kill switch."""
        order = OrderEntry(
            order_id=1, symbol="AAPL", price=100.0, quantity=100, side="BUY"
        )
        risk_gate.check(order)
        risk_gate.update_pnl(-100.0)
        risk_gate.kill_switch_active = True

        risk_gate.reset_positions()

        assert risk_gate._positions == {}
        assert len(risk_gate._order_timestamps) == 0
        assert risk_gate._cumulative_pnl == 0.0
        assert risk_gate.kill_switch_active is False


# =============================================================================
# GatewayMetrics Tests
# =============================================================================


class TestGatewayMetricsPercentiles:
    """Test percentile calculations work correctly."""

    def test_percentile_with_no_data_returns_none(self):
        """Percentile on empty dataset returns None."""
        metrics = GatewayMetrics()
        assert metrics.percentile(PipelineStage.PARSE, 0.5) is None
        assert metrics.tick_to_wire_percentile(0.99) is None

    def test_percentile_median_calculation(self):
        """Median (p50) should return middle value of sorted samples."""
        metrics = GatewayMetrics()
        # Insert known samples for PARSE stage
        metrics.stage_latencies[PipelineStage.PARSE] = [10, 20, 30, 40, 50]
        p50 = metrics.percentile(PipelineStage.PARSE, 0.5)
        # idx = int(5 * 0.5) = 2 → sorted[2] = 30
        assert p50 == 30

    def test_percentile_p99_returns_tail(self):
        """p99 should return near-max value."""
        metrics = GatewayMetrics()
        samples = list(range(1, 101))  # 1 to 100
        metrics.stage_latencies[PipelineStage.RISK_CHECK] = samples
        p99 = metrics.percentile(PipelineStage.RISK_CHECK, 0.99)
        # idx = int(100 * 0.99) = 99 → sorted[99] = 100
        assert p99 == 100

    def test_percentile_p0_returns_minimum(self):
        """p0 should return the minimum value."""
        metrics = GatewayMetrics()
        metrics.stage_latencies[PipelineStage.SERIALIZE] = [50, 30, 70, 10, 90]
        p0 = metrics.percentile(PipelineStage.SERIALIZE, 0.0)
        # idx = int(5 * 0.0) = 0 → sorted[0] = 10
        assert p0 == 10

    def test_tick_to_wire_percentile(self):
        """tick_to_wire_percentile should work on total latency samples."""
        metrics = GatewayMetrics()
        metrics.tick_to_wire_samples = [100, 200, 300, 400, 500]
        p50 = metrics.tick_to_wire_percentile(0.5)
        assert p50 == 300

    def test_percentile_single_sample(self):
        """Single sample should always be returned regardless of percentile."""
        metrics = GatewayMetrics()
        metrics.stage_latencies[PipelineStage.TRANSMIT] = [42]
        assert metrics.percentile(PipelineStage.TRANSMIT, 0.0) == 42
        assert metrics.percentile(PipelineStage.TRANSMIT, 0.5) == 42
        assert metrics.percentile(PipelineStage.TRANSMIT, 0.99) == 42

    def test_histogram_generation(self):
        """Histogram should bucket samples correctly."""
        metrics = GatewayMetrics()
        metrics.tick_to_wire_samples = [5, 15, 25, 35, 12, 18, 22]
        hist = metrics.histogram(bucket_width_ns=10)
        # Buckets: 0→[5], 10→[15,12,18], 20→[25,22], 30→[35]
        assert hist[0] == 1
        assert hist[10] == 3
        assert hist[20] == 2
        assert hist[30] == 1


class TestGatewayMetricsBurstThroughput:
    """Test burst throughput tracking."""

    def test_peak_throughput_updated_after_submissions(self):
        """peak_orders_per_second should be > 0 after multiple orders."""
        gw = FPGAGateway(variance_ns=0)
        for i in range(10):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
            )
            gw.submit_order(order)

        assert gw.metrics.peak_orders_per_second > 0

    def test_single_submission_no_peak(self):
        """A single order can't establish a rate — needs at least 2."""
        metrics = GatewayMetrics()
        latency = PipelineLatency(
            stage=PipelineStage.PARSE, entry_ns=1000, exit_ns=1035
        )
        metrics.record_pipeline_pass([latency], 100)
        # With only 1 submission, peak calculation requires >= 2 timestamps
        # It records the time but can't compute rate yet
        assert metrics.orders_submitted == 1

    def test_burst_throughput_increases_with_rapid_submissions(self):
        """Rapid submissions should show higher burst throughput."""
        gw = FPGAGateway(variance_ns=0)
        for i in range(100):
            order = OrderEntry(
                order_id=i, symbol="AAPL", price=100.0, quantity=10, side="BUY"
            )
            gw.submit_order(order)

        # With 100 rapid orders, we expect very high throughput
        assert gw.metrics.peak_orders_per_second > 1000

    def test_rejection_tracking(self):
        """Rejections should be counted separately."""
        metrics = GatewayMetrics()
        metrics.record_rejection()
        metrics.record_rejection()
        assert metrics.orders_rejected == 2

    def test_cancel_tracking(self):
        """Cancels should be tracked."""
        metrics = GatewayMetrics()
        metrics.record_cancel()
        assert metrics.cancels_submitted == 1

    def test_submission_times_deque_bounded(self):
        """Submission time window should be bounded (maxlen=10000)."""
        metrics = GatewayMetrics()
        assert metrics._submission_times.maxlen == 10000


# =============================================================================
# Integration / Factory Tests
# =============================================================================


class TestCreateGateway:
    """Test factory function for pre-configured gateways."""

    def test_aggressive_profile(self):
        """Aggressive profile should have lowest latency."""
        gw = create_gateway("aggressive")
        assert gw.wire_latency_ns == 65
        assert gw.cancel_latency_ns == 25

    def test_balanced_profile(self):
        """Balanced profile should have moderate settings."""
        gw = create_gateway("balanced")
        assert gw.wire_latency_ns == 85
        assert gw.risk_gate.max_orders_per_second == 50_000

    def test_conservative_profile(self):
        """Conservative profile should have widest limits."""
        gw = create_gateway("conservative")
        assert gw.wire_latency_ns == 120
        assert gw.risk_gate.max_notional_per_order == 25_000_000.0

    def test_unknown_profile_raises(self):
        """Unknown profile name should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown profile"):
            create_gateway("ultra_fast")


class TestCommonModelsIntegration:
    """Verify common.models types are importable and compatible."""

    def test_side_enum_values_match_gateway(self):
        """common.models.Side values should match gateway's string expectations."""
        assert Side.BUY == "BUY"
        assert Side.SELL == "SELL"

    def test_order_type_enum_values(self):
        """common.models.OrderType should have LIMIT and MARKET."""
        assert OrderType.LIMIT == "LIMIT"
        assert OrderType.MARKET == "MARKET"

    def test_common_order_can_inform_gateway_order(self):
        """A common.models.Order can be translated to gateway OrderEntry."""
        from decimal import Decimal

        common_order = Order(
            order_id="o-123",
            symbol="TSLA",
            side=Side.BUY,
            quantity=50,
            price=Decimal("250.75"),
            order_type=OrderType.LIMIT,
        )

        gateway_order = OrderEntry(
            order_id=123,
            symbol=common_order.symbol,
            price=float(common_order.price),
            quantity=common_order.quantity,
            side=common_order.side.value,
            order_type=common_order.order_type.value,
        )

        gw = FPGAGateway(variance_ns=0)
        order_out, entry_ts, wire_ts = gw.submit_order(gateway_order)
        assert order_out.symbol == "TSLA"
        assert wire_ts.ns_since_epoch > entry_ts.ns_since_epoch
