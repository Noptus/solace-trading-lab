"""Comprehensive tests for src/wire/protocol.py — SBE codec, SPSC ring buffer,
hot/cold path separation, and tick-to-trade latency measurement.
"""

from __future__ import annotations

import time

import pytest

from wire.protocol import (
    ColdPath,
    ExecutionReport,
    HotPath,
    MarketDataIncremental,
    MessageType,
    Order,
    OrderType,
    PositionSnapshot,
    SBEMessage,
    Side,
    SPSCRingBuffer,
    TickToTrade,
    TickToTradeResult,
    TickToTradeStage,
    TimeInForce,
    _SBE_HEADER_SIZE,
    _TEMPLATE_SIZES,
)


# ===========================================================================
# 1. SBEMessage Tests
# ===========================================================================


class TestSBEMessage:
    """SBE encode/decode round-trip and message size determinism."""

    @pytest.fixture
    def codec(self) -> SBEMessage:
        return SBEMessage()

    @pytest.fixture
    def sample_order(self) -> Order:
        return Order(
            order_id=123456789,
            symbol=b"AAPL    ",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            quantity=100,
            price=1_500_000,  # $150.0000 fixed-point
            timestamp_ns=1_700_000_000_000_000_000,
        )

    @pytest.fixture
    def sample_exec_report(self) -> ExecutionReport:
        return ExecutionReport(
            exec_id=987654321,
            order_id=123456789,
            symbol=b"TSLA    ",
            side=Side.SELL,
            filled_qty=50,
            fill_price=2_450_000,
            leaves_qty=50,
            timestamp_ns=1_700_000_000_000_000_001,
        )

    @pytest.fixture
    def sample_market_data(self) -> MarketDataIncremental:
        return MarketDataIncremental(
            symbol=b"MSFT    ",
            bid_price=3_750_000,
            bid_qty=200,
            ask_price=3_751_000,
            ask_qty=150,
            sequence=42,
            timestamp_ns=1_700_000_000_000_000_002,
        )

    # ----- NewOrderSingle round-trip -----

    def test_new_order_single_roundtrip_preserves_all_fields(
        self, codec: SBEMessage, sample_order: Order
    ) -> None:
        """Encode → decode roundtrip preserves every field of NewOrderSingle."""
        raw = codec.encode(sample_order)
        decoded = codec.decode(raw)

        assert isinstance(decoded, Order)
        assert decoded.order_id == sample_order.order_id
        assert decoded.symbol == sample_order.symbol
        assert decoded.side == sample_order.side
        assert decoded.order_type == sample_order.order_type
        assert decoded.time_in_force == sample_order.time_in_force
        assert decoded.quantity == sample_order.quantity
        assert decoded.price == sample_order.price
        assert decoded.timestamp_ns == sample_order.timestamp_ns

    def test_new_order_single_fixed_message_size(
        self, codec: SBEMessage, sample_order: Order
    ) -> None:
        """NewOrderSingle always produces the exact deterministic wire size."""
        raw = codec.encode(sample_order)
        expected_size = _TEMPLATE_SIZES[MessageType.NEW_ORDER_SINGLE]
        assert len(raw) == expected_size

    def test_new_order_single_header_fields(
        self, codec: SBEMessage, sample_order: Order
    ) -> None:
        """Header contains correct message_type, block_length, template_id, schema, version."""
        import struct

        raw = codec.encode(sample_order)
        msg_type, block_length, template_id, schema_id, version = struct.unpack(
            "!HHHHH", raw[:_SBE_HEADER_SIZE]
        )
        assert msg_type == MessageType.NEW_ORDER_SINGLE
        assert template_id == MessageType.NEW_ORDER_SINGLE
        assert schema_id == 1
        assert version == 1
        # block_length should equal body size (total - header)
        assert block_length == len(raw) - _SBE_HEADER_SIZE

    # ----- CancelOrder round-trip -----

    def test_cancel_order_roundtrip(self, codec: SBEMessage) -> None:
        """CancelOrder encode/decode preserves order_id, symbol, timestamp."""
        order = Order(
            order_id=555,
            symbol=b"GOOG    ",
            timestamp_ns=9_999_999_999,
        )
        raw = codec.encode(order, msg_type=MessageType.CANCEL_ORDER)
        decoded = codec.decode(raw)

        assert isinstance(decoded, Order)
        assert decoded.order_id == 555
        assert decoded.symbol == b"GOOG    "
        assert decoded.timestamp_ns == 9_999_999_999

    def test_cancel_order_fixed_size(self, codec: SBEMessage) -> None:
        """CancelOrder wire size matches template spec."""
        order = Order(order_id=1, symbol=b"X       ", timestamp_ns=0)
        raw = codec.encode(order, msg_type=MessageType.CANCEL_ORDER)
        assert len(raw) == _TEMPLATE_SIZES[MessageType.CANCEL_ORDER]

    # ----- ExecutionReport round-trip -----

    def test_execution_report_roundtrip(
        self, codec: SBEMessage, sample_exec_report: ExecutionReport
    ) -> None:
        """ExecutionReport round-trip preserves all fields."""
        raw = codec.encode(sample_exec_report)
        decoded = codec.decode(raw)

        assert isinstance(decoded, ExecutionReport)
        assert decoded.exec_id == sample_exec_report.exec_id
        assert decoded.order_id == sample_exec_report.order_id
        assert decoded.symbol == sample_exec_report.symbol
        assert decoded.side == sample_exec_report.side
        assert decoded.filled_qty == sample_exec_report.filled_qty
        assert decoded.fill_price == sample_exec_report.fill_price
        assert decoded.leaves_qty == sample_exec_report.leaves_qty
        assert decoded.timestamp_ns == sample_exec_report.timestamp_ns

    def test_execution_report_fixed_size(
        self, codec: SBEMessage, sample_exec_report: ExecutionReport
    ) -> None:
        """ExecutionReport wire size matches template spec."""
        raw = codec.encode(sample_exec_report)
        assert len(raw) == _TEMPLATE_SIZES[MessageType.EXECUTION_REPORT]

    # ----- MarketDataIncremental round-trip -----

    def test_market_data_incremental_roundtrip(
        self, codec: SBEMessage, sample_market_data: MarketDataIncremental
    ) -> None:
        """MarketDataIncremental round-trip preserves all fields."""
        raw = codec.encode(sample_market_data)
        decoded = codec.decode(raw)

        assert isinstance(decoded, MarketDataIncremental)
        assert decoded.symbol == sample_market_data.symbol
        assert decoded.bid_price == sample_market_data.bid_price
        assert decoded.bid_qty == sample_market_data.bid_qty
        assert decoded.ask_price == sample_market_data.ask_price
        assert decoded.ask_qty == sample_market_data.ask_qty
        assert decoded.sequence == sample_market_data.sequence
        assert decoded.timestamp_ns == sample_market_data.timestamp_ns

    def test_market_data_incremental_fixed_size(
        self, codec: SBEMessage, sample_market_data: MarketDataIncremental
    ) -> None:
        """MarketDataIncremental wire size matches template spec."""
        raw = codec.encode(sample_market_data)
        assert len(raw) == _TEMPLATE_SIZES[MessageType.MARKET_DATA_INCREMENTAL]

    # ----- All 4 templates produce deterministic sizes -----

    def test_all_templates_deterministic_sizes(self, codec: SBEMessage) -> None:
        """Every template produces a fixed, deterministic wire size."""
        order = Order(
            order_id=1, symbol=b"TEST    ", side=Side.SELL,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.FOK,
            quantity=999, price=42_000, timestamp_ns=1,
        )
        exec_rpt = ExecutionReport(
            exec_id=2, order_id=1, symbol=b"TEST    ", side=Side.BUY,
            filled_qty=10, fill_price=42_000, leaves_qty=989, timestamp_ns=2,
        )
        md = MarketDataIncremental(
            symbol=b"TEST    ", bid_price=100, bid_qty=50,
            ask_price=101, ask_qty=50, sequence=1, timestamp_ns=3,
        )

        assert len(codec.encode(order)) == _TEMPLATE_SIZES[MessageType.NEW_ORDER_SINGLE]
        assert len(codec.encode(order, MessageType.CANCEL_ORDER)) == _TEMPLATE_SIZES[MessageType.CANCEL_ORDER]
        assert len(codec.encode(exec_rpt)) == _TEMPLATE_SIZES[MessageType.EXECUTION_REPORT]
        assert len(codec.encode(md)) == _TEMPLATE_SIZES[MessageType.MARKET_DATA_INCREMENTAL]

    # ----- Error cases -----

    def test_decode_truncated_frame_raises(self, codec: SBEMessage) -> None:
        """Decode raises ValueError on truncated frame."""
        with pytest.raises(ValueError, match="Frame too short"):
            codec.decode(b"\x00" * 5)

    def test_decode_truncated_body_raises(self, codec: SBEMessage) -> None:
        """Decode raises ValueError if body is too short for declared block_length."""
        import struct
        # Header claiming 100-byte body but only 5 bytes present
        header = struct.pack("!HHHHH", 1, 100, 1, 1, 1)
        with pytest.raises(ValueError, match="body truncated"):
            codec.decode(header + b"\x00" * 5)

    def test_encode_unsupported_type_raises(self, codec: SBEMessage) -> None:
        """Encode raises ValueError for unsupported object types."""
        with pytest.raises((ValueError, TypeError, AssertionError)):
            codec.encode("not_an_order")  # type: ignore[arg-type]


# ===========================================================================
# 2. SPSCRingBuffer Tests
# ===========================================================================


class TestSPSCRingBuffer:
    """Lock-free single-producer single-consumer ring buffer."""

    def test_capacity_is_power_of_two(self) -> None:
        """Requested capacity is rounded up to the nearest power of 2."""
        rb = SPSCRingBuffer(capacity=100)
        assert rb.capacity == 128  # next power of 2 >= 100
        assert (rb.capacity & (rb.capacity - 1)) == 0  # is power of 2

    def test_capacity_exact_power_of_two_unchanged(self) -> None:
        """Exact power-of-2 capacity is not rounded further."""
        rb = SPSCRingBuffer(capacity=256)
        assert rb.capacity == 256

    def test_publish_consume_fifo_order(self) -> None:
        """Messages are consumed in FIFO order."""
        rb = SPSCRingBuffer(capacity=8, slot_size=16)

        rb.try_publish(b"msg_1")
        rb.try_publish(b"msg_2")
        rb.try_publish(b"msg_3")

        out1 = rb.try_consume()
        out2 = rb.try_consume()
        out3 = rb.try_consume()

        assert out1 is not None and out1[:5] == b"msg_1"
        assert out2 is not None and out2[:5] == b"msg_2"
        assert out3 is not None and out3[:5] == b"msg_3"

    def test_full_buffer_returns_false(self) -> None:
        """Publishing to a full buffer returns False (back-pressure)."""
        # Capacity 4 → usable slots = capacity - 1 = 3 (one slot sentinel)
        rb = SPSCRingBuffer(capacity=4, slot_size=8)

        # Fill all usable slots
        results = []
        for i in range(10):
            results.append(rb.try_publish(f"m{i}".encode()))

        # At least one must be False (buffer full)
        assert False in results
        # First publishes succeed
        assert results[0] is True

    def test_empty_buffer_consume_returns_none(self) -> None:
        """Consuming from empty buffer returns None."""
        rb = SPSCRingBuffer(capacity=8, slot_size=16)
        assert rb.try_consume() is None

    def test_size_tracking(self) -> None:
        """Size property correctly tracks pending items."""
        rb = SPSCRingBuffer(capacity=16, slot_size=8)
        assert rb.size == 0
        assert rb.is_empty

        rb.try_publish(b"a")
        rb.try_publish(b"b")
        assert rb.size == 2
        assert not rb.is_empty

        rb.try_consume()
        assert rb.size == 1

    def test_wrap_around(self) -> None:
        """Buffer wraps around correctly after many publish/consume cycles."""
        rb = SPSCRingBuffer(capacity=4, slot_size=8)

        # Run more cycles than capacity to force wrap-around
        for i in range(20):
            data = f"x{i:04d}".encode()
            assert rb.try_publish(data) is True
            consumed = rb.try_consume()
            assert consumed is not None
            assert consumed[: len(data)] == data

    def test_slot_data_isolation(self) -> None:
        """Data in one slot doesn't bleed into another."""
        rb = SPSCRingBuffer(capacity=8, slot_size=8)
        rb.try_publish(b"\xff" * 8)
        rb.try_publish(b"\x00" * 4)

        _ = rb.try_consume()
        msg2 = rb.try_consume()
        # Remainder of slot should be zeroed
        assert msg2 is not None
        assert msg2 == b"\x00" * 4 + b"\x00" * 4


# ===========================================================================
# 3. HotPath Tests
# ===========================================================================


class TestHotPath:
    """Hot path: zero-allocation pipeline simulation."""

    @pytest.fixture
    def hot_path(self) -> HotPath:
        return HotPath(pool_size=64, buffer_size=128, ring_capacity=128, pinned_core=3)

    def test_process_returns_bytes_same_or_smaller_size(self, hot_path: HotPath) -> None:
        """process() returns bytes of same or smaller size than input."""
        test_data = b"\x01\x02\x03\x04\x05" * 10  # 50 bytes < 128 buffer
        result = hot_path.process(test_data)
        assert isinstance(result, bytes)
        assert len(result) <= len(test_data)
        assert result == test_data  # exact echo for data within buffer

    def test_process_truncates_oversized_input(self, hot_path: HotPath) -> None:
        """process() truncates input larger than buffer_size."""
        oversized = b"\xAB" * 256  # larger than 128 buffer_size
        result = hot_path.process(oversized)
        assert len(result) == 128  # capped at buffer_size

    def test_buffer_pool_has_correct_slots(self) -> None:
        """Buffer pool is pre-allocated with the exact requested number of slots."""
        hp = HotPath(pool_size=512, buffer_size=64)
        assert len(hp._buffer_pool) == 512
        assert all(len(buf) == 64 for buf in hp._buffer_pool)

    def test_no_allocation_on_hot_path_conceptual(self, hot_path: HotPath) -> None:
        """Conceptual test: process() reuses pre-allocated buffers without growing the pool.

        We verify the pool size remains constant after processing many messages,
        demonstrating the zero-allocation design pattern.
        """
        pool_size_before = len(hot_path._buffer_pool)

        # Process many messages
        for i in range(1000):
            hot_path.process(f"order_{i:08d}".encode())

        pool_size_after = len(hot_path._buffer_pool)
        assert pool_size_after == pool_size_before, "Pool grew — allocation on hot path!"

    def test_ring_buffer_receives_messages(self, hot_path: HotPath) -> None:
        """Hot path hands off messages to cold path via ring buffer."""
        data = b"test_message_for_cold_path"
        hot_path.process(data)

        # Cold path ring should have the message
        consumed = hot_path.ring_to_cold.try_consume()
        assert consumed is not None
        assert consumed[: len(data)] == data

    def test_processed_count_increments(self, hot_path: HotPath) -> None:
        """processed_count tracks total messages processed."""
        assert hot_path.processed_count == 0
        hot_path.process(b"msg1")
        hot_path.process(b"msg2")
        assert hot_path.processed_count == 2

    def test_cpu_affinity_configured(self, hot_path: HotPath) -> None:
        """CPU affinity is set to the pinned core."""
        assert hot_path.cpu_affinity.core_id == 3
        assert hot_path.cpu_affinity.isolated is True


# ===========================================================================
# 4. ColdPath Tests
# ===========================================================================


class TestColdPath:
    """Cold path: async logging, position reporting, ring draining."""

    @pytest.fixture
    def ring(self) -> SPSCRingBuffer:
        return SPSCRingBuffer(capacity=64, slot_size=128)

    @pytest.fixture
    def cold_path(self, ring: SPSCRingBuffer) -> ColdPath:
        return ColdPath(ring_from_hot=ring, log_batch_size=10)

    def test_log_event_accumulates_events(self, cold_path: ColdPath) -> None:
        """log_event accumulates events in the internal buffer."""
        cold_path.log_event(b"event_1")
        cold_path.log_event(b"event_2")
        cold_path.log_event(b"event_3")

        # Events are buffered (not flushed yet since batch_size=10)
        assert len(cold_path._log_buffer) == 3

    def test_log_event_flushes_at_batch_size(self, cold_path: ColdPath) -> None:
        """log_event flushes when batch_size is reached."""
        for i in range(10):
            cold_path.log_event(f"event_{i}".encode())

        # After reaching batch_size=10, buffer should be flushed (cleared)
        assert len(cold_path._log_buffer) == 0

    def test_drain_from_hot_path(self, ring: SPSCRingBuffer) -> None:
        """Cold path drains messages published to the ring by hot path."""
        cold = ColdPath(ring_from_hot=ring, log_batch_size=100)

        # Simulate hot path publishing
        ring.try_publish(b"hot_msg_1")
        ring.try_publish(b"hot_msg_2")
        ring.try_publish(b"hot_msg_3")

        # Start cold path and let it drain
        cold.start()
        time.sleep(0.05)  # Allow drain thread to process
        cold.stop()

        assert cold.event_count == 3

    def test_drain_from_hot_path_via_hotpath_object(self) -> None:
        """End-to-end: HotPath → ring → ColdPath drain."""
        hp = HotPath(pool_size=32, buffer_size=64, ring_capacity=64)
        cold = ColdPath(ring_from_hot=hp.ring_to_cold, log_batch_size=100)

        # Process messages on hot path
        for i in range(5):
            hp.process(f"order_{i}".encode())

        # Start cold path drain
        cold.start()
        time.sleep(0.05)
        cold.stop()

        assert cold.event_count == 5

    def test_position_reporting_empty(self, cold_path: ColdPath) -> None:
        """report_position returns empty list when no positions exist."""
        assert cold_path.report_position() == []
        assert cold_path.report_position(symbol=b"AAPL    ") == []

    def test_position_reporting_after_update(self, cold_path: ColdPath) -> None:
        """Position updates are reflected in report_position."""
        cold_path.update_position(symbol=b"AAPL    ", qty_delta=100, price=1_500_000)
        cold_path.update_position(symbol=b"TSLA    ", qty_delta=-50, price=2_000_000)

        all_positions = cold_path.report_position()
        assert len(all_positions) == 2

        aapl_pos = cold_path.report_position(symbol=b"AAPL    ")
        assert len(aapl_pos) == 1
        assert aapl_pos[0].net_qty == 100
        assert aapl_pos[0].avg_price == 1_500_000

    def test_position_reporting_cumulative(self, cold_path: ColdPath) -> None:
        """Multiple updates to same symbol accumulate correctly."""
        cold_path.update_position(symbol=b"AAPL    ", qty_delta=100, price=1_500_000)
        cold_path.update_position(
            symbol=b"AAPL    ", qty_delta=-30, price=1_510_000, realized_pnl_delta=300_000
        )

        pos = cold_path.report_position(symbol=b"AAPL    ")
        assert len(pos) == 1
        assert pos[0].net_qty == 70  # 100 - 30
        assert pos[0].realized_pnl == 300_000

    def test_stop_without_start(self, cold_path: ColdPath) -> None:
        """Stopping a cold path that was never started does not crash."""
        cold_path.stop()  # Should not raise


# ===========================================================================
# 5. TickToTrade Tests
# ===========================================================================


class TestTickToTrade:
    """Tick-to-trade latency measurement and percentile calculations."""

    @pytest.fixture
    def ttt(self) -> TickToTrade:
        return TickToTrade()

    def test_start_checkpoint_complete_flow(self, ttt: TickToTrade) -> None:
        """Basic flow: start → checkpoints → complete returns a valid result."""
        event_id = 1
        ttt.start_measurement(event_id)
        ttt.checkpoint(event_id, TickToTradeStage.SIGNAL_COMPUTE)
        ttt.checkpoint(event_id, TickToTradeStage.ORDER_BUILD)
        ttt.checkpoint(event_id, TickToTradeStage.RISK_CHECK)
        ttt.checkpoint(event_id, TickToTradeStage.WIRE_ENCODE)
        result = ttt.complete(event_id)

        assert isinstance(result, TickToTradeResult)
        assert result.event_id == event_id
        assert result.total_ns > 0
        assert "MARKET_DATA_RECV" in result.stage_ns
        assert "NIC_EGRESS" in result.stage_ns

    def test_all_six_stages_tracked(self, ttt: TickToTrade) -> None:
        """All 6 pipeline stages appear in the result breakdown."""
        event_id = 42
        ttt.start_measurement(event_id)
        ttt.checkpoint(event_id, TickToTradeStage.SIGNAL_COMPUTE)
        ttt.checkpoint(event_id, TickToTradeStage.ORDER_BUILD)
        ttt.checkpoint(event_id, TickToTradeStage.RISK_CHECK)
        ttt.checkpoint(event_id, TickToTradeStage.WIRE_ENCODE)
        result = ttt.complete(event_id)

        expected_stages = {
            "MARKET_DATA_RECV",
            "SIGNAL_COMPUTE",
            "ORDER_BUILD",
            "RISK_CHECK",
            "WIRE_ENCODE",
            "NIC_EGRESS",
        }
        assert set(result.stage_ns.keys()) == expected_stages

    def test_stage_enum_has_six_values(self) -> None:
        """TickToTradeStage enum defines exactly 6 stages."""
        stages = list(TickToTradeStage)
        assert len(stages) == 6
        assert TickToTradeStage.MARKET_DATA_RECV in stages
        assert TickToTradeStage.SIGNAL_COMPUTE in stages
        assert TickToTradeStage.ORDER_BUILD in stages
        assert TickToTradeStage.RISK_CHECK in stages
        assert TickToTradeStage.WIRE_ENCODE in stages
        assert TickToTradeStage.NIC_EGRESS in stages

    def test_percentiles_with_known_data(self, ttt: TickToTrade) -> None:
        """Percentiles are computed correctly with controlled latency values."""
        # Inject known latencies directly for deterministic test
        known_latencies = list(range(1, 101))  # 1ns to 100ns
        ttt._latencies_ns = known_latencies

        p50 = ttt.percentile(50.0)
        p99 = ttt.percentile(99.0)
        p999 = ttt.percentile(99.9)

        # p50 should be around 50
        assert 49 <= p50 <= 51
        # p99 should be around 99
        assert 98 <= p99 <= 100
        # p999 should be at the high end
        assert 99 <= p999 <= 100

    def test_p50_p99_p999_properties(self, ttt: TickToTrade) -> None:
        """p50_ns, p99_ns, p999_ns properties return correct percentiles."""
        # Inject latencies
        ttt._latencies_ns = list(range(100, 1100))  # 100ns to 1099ns, 1000 values

        p50 = ttt.p50_ns
        p99 = ttt.p99_ns
        p999 = ttt.p999_ns

        # Percentiles should be monotonically increasing
        assert p50 <= p99 <= p999
        # p50 ~ 600ns (middle of 100..1099)
        assert 500 <= p50 <= 700
        # p99 ~ 1090ns
        assert 1050 <= p99 <= 1100
        # p999 ~ 1099ns
        assert 1090 <= p999 <= 1100

    def test_percentile_no_data_raises(self, ttt: TickToTrade) -> None:
        """percentile raises ValueError when no measurements exist."""
        with pytest.raises(ValueError, match="No completed measurements"):
            ttt.percentile(50.0)

    def test_multiple_concurrent_measurements(self, ttt: TickToTrade) -> None:
        """Multiple in-flight measurements tracked independently."""
        ttt.start_measurement(1)
        ttt.start_measurement(2)
        ttt.start_measurement(3)

        ttt.checkpoint(2, TickToTradeStage.SIGNAL_COMPUTE)
        ttt.checkpoint(1, TickToTradeStage.SIGNAL_COMPUTE)
        ttt.checkpoint(3, TickToTradeStage.SIGNAL_COMPUTE)

        ttt.checkpoint(1, TickToTradeStage.ORDER_BUILD)
        ttt.checkpoint(2, TickToTradeStage.ORDER_BUILD)
        ttt.checkpoint(3, TickToTradeStage.ORDER_BUILD)

        # Complete in different order
        r2 = ttt.complete(2)
        r1 = ttt.complete(1)
        r3 = ttt.complete(3)

        assert r1.event_id == 1
        assert r2.event_id == 2
        assert r3.event_id == 3
        assert ttt.count == 3

    def test_complete_unknown_event_raises(self, ttt: TickToTrade) -> None:
        """Completing an unknown event_id raises KeyError."""
        with pytest.raises(KeyError):
            ttt.complete(999)

    def test_checkpoint_unknown_event_raises(self, ttt: TickToTrade) -> None:
        """Checkpointing an unknown event_id raises KeyError."""
        with pytest.raises(KeyError):
            ttt.checkpoint(999, TickToTradeStage.SIGNAL_COMPUTE)

    def test_meets_target_flag(self, ttt: TickToTrade) -> None:
        """meets_target is True when total_ns < 1000 (1µs target)."""
        # The actual Python overhead will likely exceed 1µs,
        # but we can test the logic by injecting known total
        ttt.start_measurement(1)
        result = ttt.complete(1)
        # Result should have meets_target set based on actual measured time
        assert isinstance(result.meets_target, bool)
        # total_ns should be consistent with meets_target
        if result.total_ns < 1000:
            assert result.meets_target is True
        else:
            assert result.meets_target is False

    def test_reset_clears_all_state(self, ttt: TickToTrade) -> None:
        """reset() clears all measurements and statistics."""
        ttt.start_measurement(1)
        ttt.complete(1)
        assert ttt.count == 1

        ttt.reset()
        assert ttt.count == 0
        assert ttt.mean_ns == 0.0

    def test_histogram_buckets(self, ttt: TickToTrade) -> None:
        """histogram() groups latencies into correct buckets."""
        ttt._latencies_ns = [50, 150, 250, 251, 350, 999]
        hist = ttt.histogram(bucket_ns=100)

        assert hist[0] == 1    # 50ns → bucket 0
        assert hist[100] == 1  # 150ns → bucket 100
        assert hist[200] == 2  # 250, 251 → bucket 200
        assert hist[300] == 1  # 350ns → bucket 300
        assert hist[900] == 1  # 999ns → bucket 900

    def test_summary_string(self, ttt: TickToTrade) -> None:
        """summary() returns a formatted string with stats."""
        ttt._latencies_ns = [100, 200, 300]
        ttt._completed = [
            TickToTradeResult(event_id=i, total_ns=v, stage_ns={}, meets_target=True)
            for i, v in enumerate([100, 200, 300])
        ]
        s = ttt.summary()
        assert "TickToTrade" in s
        assert "n=3" in s
        assert "p50=" in s
        assert "p99=" in s


# ===========================================================================
# Integration: SBE + HotPath + ColdPath round-trip
# ===========================================================================


class TestIntegration:
    """End-to-end flow through the wire protocol pipeline."""

    def test_full_pipeline_order_flow(self) -> None:
        """Order encoded → hot path → cold path drain → decode preserves data."""
        codec = SBEMessage()
        hot = HotPath(pool_size=32, buffer_size=128, ring_capacity=64)
        cold = ColdPath(ring_from_hot=hot.ring_to_cold, log_batch_size=100)

        # Encode an order
        order = Order(
            order_id=777,
            symbol=b"NVDA    ",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=250,
            price=8_000_000,
            timestamp_ns=1_700_000_000_000,
        )
        raw = codec.encode(order)

        # Process on hot path
        result = hot.process(raw)

        # Decode from hot path output
        decoded = codec.decode(result)
        assert isinstance(decoded, Order)
        assert decoded.order_id == 777
        assert decoded.symbol == b"NVDA    "
        assert decoded.quantity == 250

        # Cold path should have the message in ring
        cold.start()
        time.sleep(0.05)
        cold.stop()
        assert cold.event_count == 1
