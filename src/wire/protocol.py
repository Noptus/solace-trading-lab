"""Custom SBE-inspired binary framing and hot/cold-path simulation.

Buffer pools, a ring and timing counters illustrate design patterns. Python
operations allocate; concurrency and lock-free guarantees are not established.
The header is custom and has not been validated for FIX SBE interoperability.
No hardware performance or trading-firm benchmark is claimed.
"""

from __future__ import annotations

import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Self


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class Side(IntEnum):
    """Order side."""

    BUY = 1
    SELL = 2


class OrderType(IntEnum):
    """Order type."""

    MARKET = 1
    LIMIT = 2
    STOP = 3


class TimeInForce(IntEnum):
    """Time-in-force qualifier."""

    DAY = 0
    GTC = 1
    IOC = 2
    FOK = 3


@dataclass(slots=True)
class Order:
    """Represents an order in the trading system.

    Fixed-width fields only — no variable-length strings on the hot path.
    Symbol is encoded as an 8-byte padded ASCII field.
    """

    order_id: int = 0
    symbol: bytes = b"\x00" * 8  # 8-byte fixed symbol (e.g. b'AAPL    ')
    side: Side = Side.BUY
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.DAY
    quantity: int = 0
    price: int = 0  # price in fixed-point (price * 10_000)
    timestamp_ns: int = 0


@dataclass(slots=True)
class ExecutionReport:
    """Execution report (fill or ack)."""

    exec_id: int = 0
    order_id: int = 0
    symbol: bytes = b"\x00" * 8
    side: Side = Side.BUY
    filled_qty: int = 0
    fill_price: int = 0  # fixed-point
    leaves_qty: int = 0
    timestamp_ns: int = 0


@dataclass(slots=True)
class MarketDataIncremental:
    """Incremental market data update (L1 BBO)."""

    symbol: bytes = b"\x00" * 8
    bid_price: int = 0  # fixed-point
    bid_qty: int = 0
    ask_price: int = 0  # fixed-point
    ask_qty: int = 0
    sequence: int = 0
    timestamp_ns: int = 0


# ---------------------------------------------------------------------------
# SBE Message Templates
# ---------------------------------------------------------------------------


class MessageType(IntEnum):
    """SBE message template identifiers."""

    NEW_ORDER_SINGLE = 1
    CANCEL_ORDER = 2
    EXECUTION_REPORT = 3
    MARKET_DATA_INCREMENTAL = 4


# SBE header: message_type(2) + block_length(2) + template_id(2) + schema_id(2) + version(2) = 10 bytes
_SBE_HEADER_FMT = "!HHHHH"
_SBE_HEADER_SIZE = struct.calcsize(_SBE_HEADER_FMT)

# NewOrderSingle body: order_id(8) + symbol(8) + side(1) + order_type(1) + tif(1) + pad(1) + qty(4) + price(8) + ts(8) = 40
_NEW_ORDER_FMT = "!Q8sBBBxIqQ"
_NEW_ORDER_SIZE = struct.calcsize(_NEW_ORDER_FMT)

# CancelOrder body: order_id(8) + symbol(8) + timestamp(8) = 24
_CANCEL_ORDER_FMT = "!Q8sQ"
_CANCEL_ORDER_SIZE = struct.calcsize(_CANCEL_ORDER_FMT)

# ExecutionReport body: exec_id(8) + order_id(8) + symbol(8) + side(1) + pad(3) + filled_qty(4) + fill_price(8) + leaves_qty(4) + pad(4) + ts(8) = 56
_EXEC_REPORT_FMT = "!QQ8sBxxxIqIxxxxQ"
_EXEC_REPORT_SIZE = struct.calcsize(_EXEC_REPORT_FMT)

# MarketDataIncremental body: symbol(8) + bid_price(8) + bid_qty(4) + ask_price(8) + ask_qty(4) + sequence(8) + ts(8) = 48
_MD_INCREMENTAL_FMT = "!8sqIqIQQ"
_MD_INCREMENTAL_SIZE = struct.calcsize(_MD_INCREMENTAL_FMT)

_SCHEMA_ID: int = 1
_VERSION: int = 1

# Template size lookup for deterministic allocation
_TEMPLATE_SIZES: dict[MessageType, int] = {
    MessageType.NEW_ORDER_SINGLE: _SBE_HEADER_SIZE + _NEW_ORDER_SIZE,
    MessageType.CANCEL_ORDER: _SBE_HEADER_SIZE + _CANCEL_ORDER_SIZE,
    MessageType.EXECUTION_REPORT: _SBE_HEADER_SIZE + _EXEC_REPORT_SIZE,
    MessageType.MARKET_DATA_INCREMENTAL: _SBE_HEADER_SIZE + _MD_INCREMENTAL_SIZE,
}


class SBEMessage:
    """Custom fixed-width codec inspired by Simple Binary Encoding.

    Fixed-size binary encoding with no variable-length fields on the hot path.
    Each template has a deterministic wire size; encoding and decoding allocate.

    Header layout (10 bytes, network byte order):
        - message_type:  uint16 — template selector
        - block_length:  uint16 — body size in bytes (excludes header)
        - template_id:   uint16 — same as message_type (SBE convention)
        - schema_id:     uint16 — schema version namespace
        - version:       uint16 — schema version

    Supported templates:
        - NewOrderSingle (1)
        - CancelOrder (2)
        - ExecutionReport (3)
        - MarketDataIncremental (4)
    """

    __slots__ = ("_encode_ns_total", "_decode_ns_total", "_encode_count", "_decode_count")

    def __init__(self) -> None:
        self._encode_ns_total: int = 0
        self._decode_ns_total: int = 0
        self._encode_count: int = 0
        self._decode_count: int = 0

    # ------------------------------------------------------------------
    # Encode
    # ------------------------------------------------------------------

    def encode(self, order: Order | ExecutionReport | MarketDataIncremental, msg_type: MessageType | None = None) -> bytes:
        """Encode a domain object into a fixed-size SBE binary frame.

        Args:
            order: The domain object to serialize.
            msg_type: Explicit message type override. Auto-detected if None.

        Returns:
            Raw bytes of deterministic length for the given template.

        Raises:
            ValueError: If message type cannot be determined or is unsupported.
        """
        t0 = time.perf_counter_ns()

        if msg_type is None:
            if isinstance(order, Order):
                msg_type = MessageType.NEW_ORDER_SINGLE
            elif isinstance(order, ExecutionReport):
                msg_type = MessageType.EXECUTION_REPORT
            elif isinstance(order, MarketDataIncremental):
                msg_type = MessageType.MARKET_DATA_INCREMENTAL
            else:
                raise ValueError(f"Cannot auto-detect message type for {type(order)}")

        if msg_type == MessageType.NEW_ORDER_SINGLE:
            assert isinstance(order, Order)
            body = struct.pack(
                _NEW_ORDER_FMT,
                order.order_id,
                order.symbol,
                order.side,
                order.order_type,
                order.time_in_force,
                order.quantity,
                order.price,
                order.timestamp_ns,
            )
            block_length = _NEW_ORDER_SIZE

        elif msg_type == MessageType.CANCEL_ORDER:
            assert isinstance(order, Order)
            body = struct.pack(
                _CANCEL_ORDER_FMT,
                order.order_id,
                order.symbol,
                order.timestamp_ns,
            )
            block_length = _CANCEL_ORDER_SIZE

        elif msg_type == MessageType.EXECUTION_REPORT:
            assert isinstance(order, ExecutionReport)
            body = struct.pack(
                _EXEC_REPORT_FMT,
                order.exec_id,
                order.order_id,
                order.symbol,
                order.side,
                order.filled_qty,
                order.fill_price,
                order.leaves_qty,
                order.timestamp_ns,
            )
            block_length = _EXEC_REPORT_SIZE

        elif msg_type == MessageType.MARKET_DATA_INCREMENTAL:
            assert isinstance(order, MarketDataIncremental)
            body = struct.pack(
                _MD_INCREMENTAL_FMT,
                order.symbol,
                order.bid_price,
                order.bid_qty,
                order.ask_price,
                order.ask_qty,
                order.sequence,
                order.timestamp_ns,
            )
            block_length = _MD_INCREMENTAL_SIZE

        else:
            raise ValueError(f"Unsupported message type: {msg_type}")

        header = struct.pack(
            _SBE_HEADER_FMT,
            msg_type,
            block_length,
            msg_type,  # template_id mirrors message_type
            _SCHEMA_ID,
            _VERSION,
        )

        result = header + body

        elapsed = time.perf_counter_ns() - t0
        self._encode_ns_total += elapsed
        self._encode_count += 1

        return result

    # ------------------------------------------------------------------
    # Decode
    # ------------------------------------------------------------------

    def decode(self, raw: bytes) -> Order | ExecutionReport | MarketDataIncremental:
        """Decode raw SBE bytes into a domain object.

        Args:
            raw: Complete SBE frame (header + body).

        Returns:
            Decoded domain object matching the template.

        Raises:
            ValueError: If message type in header is unknown or frame is truncated.
        """
        t0 = time.perf_counter_ns()

        if len(raw) < _SBE_HEADER_SIZE:
            raise ValueError(f"Frame too short for header: {len(raw)} < {_SBE_HEADER_SIZE}")

        msg_type, block_length, _template_id, _schema_id, _version = struct.unpack(
            _SBE_HEADER_FMT, raw[:_SBE_HEADER_SIZE]
        )

        body = raw[_SBE_HEADER_SIZE:]

        if len(body) < block_length:
            raise ValueError(f"Frame body truncated: {len(body)} < {block_length}")

        result: Order | ExecutionReport | MarketDataIncremental

        if msg_type == MessageType.NEW_ORDER_SINGLE:
            order_id, symbol, side, order_type, tif, qty, price, ts = struct.unpack(
                _NEW_ORDER_FMT, body[:_NEW_ORDER_SIZE]
            )
            result = Order(
                order_id=order_id,
                symbol=symbol,
                side=Side(side),
                order_type=OrderType(order_type),
                time_in_force=TimeInForce(tif),
                quantity=qty,
                price=price,
                timestamp_ns=ts,
            )

        elif msg_type == MessageType.CANCEL_ORDER:
            order_id, symbol, ts = struct.unpack(_CANCEL_ORDER_FMT, body[:_CANCEL_ORDER_SIZE])
            result = Order(order_id=order_id, symbol=symbol, timestamp_ns=ts)

        elif msg_type == MessageType.EXECUTION_REPORT:
            exec_id, order_id, symbol, side, filled_qty, fill_price, leaves_qty, ts = struct.unpack(
                _EXEC_REPORT_FMT, body[:_EXEC_REPORT_SIZE]
            )
            result = ExecutionReport(
                exec_id=exec_id,
                order_id=order_id,
                symbol=symbol,
                side=Side(side),
                filled_qty=filled_qty,
                fill_price=fill_price,
                leaves_qty=leaves_qty,
                timestamp_ns=ts,
            )

        elif msg_type == MessageType.MARKET_DATA_INCREMENTAL:
            symbol, bid_price, bid_qty, ask_price, ask_qty, seq, ts = struct.unpack(
                _MD_INCREMENTAL_FMT, body[:_MD_INCREMENTAL_SIZE]
            )
            result = MarketDataIncremental(
                symbol=symbol,
                bid_price=bid_price,
                bid_qty=bid_qty,
                ask_price=ask_price,
                ask_qty=ask_qty,
                sequence=seq,
                timestamp_ns=ts,
            )

        else:
            raise ValueError(f"Unknown message type in header: {msg_type}")

        elapsed = time.perf_counter_ns() - t0
        self._decode_ns_total += elapsed
        self._decode_count += 1

        return result

    # ------------------------------------------------------------------
    # Benchmark accessors
    # ------------------------------------------------------------------

    @property
    def avg_encode_ns(self) -> float:
        """Average encode latency in nanoseconds."""
        return self._encode_ns_total / max(self._encode_count, 1)

    @property
    def avg_decode_ns(self) -> float:
        """Average decode latency in nanoseconds."""
        return self._decode_ns_total / max(self._decode_count, 1)

    def reset_stats(self) -> None:
        """Reset encode/decode timing accumulators."""
        self._encode_ns_total = 0
        self._decode_ns_total = 0
        self._encode_count = 0
        self._decode_count = 0


# ---------------------------------------------------------------------------
# SPSC Ring Buffer Model
# ---------------------------------------------------------------------------


class SPSCRingBuffer:
    """Single-producer/single-consumer ring buffer model.

    Uses Python indices and pre-allocated byte slots. It has no verified
    cross-thread memory-ordering or lock-free progress guarantee.

    The buffer stores fixed-size byte slots (pre-allocated).
    """

    __slots__ = ("_capacity", "_mask", "_buffer", "_write_idx", "_read_idx", "_slot_size")

    def __init__(self, capacity: int = 1024, slot_size: int = 128) -> None:
        """Initialize ring buffer with power-of-2 capacity.

        Args:
            capacity: Number of slots (rounded up to next power of 2).
            slot_size: Fixed byte size per slot.
        """
        # Round up to power of 2
        actual_cap = 1
        while actual_cap < capacity:
            actual_cap <<= 1
        self._capacity: int = actual_cap
        self._mask: int = actual_cap - 1
        self._slot_size: int = slot_size
        # Pre-allocate storage; Python operations can still allocate
        self._buffer: list[bytearray] = [bytearray(slot_size) for _ in range(actual_cap)]
        self._write_idx: int = 0
        self._read_idx: int = 0

    def try_publish(self, data: bytes) -> bool:
        """Attempt to publish data to the ring buffer (producer side).

        Returns False if the buffer is full (back-pressure).
        Copies into a pre-allocated slot; Python slices may allocate.

        Args:
            data: Raw bytes to enqueue (must be <= slot_size).

        Returns:
            True if published, False if full.
        """
        next_write = (self._write_idx + 1) & self._mask
        if next_write == self._read_idx:
            return False  # Full — back-pressure

        slot = self._buffer[self._write_idx & self._mask]
        n = min(len(data), self._slot_size)
        slot[:n] = data[:n]
        # Zero remainder to avoid stale data leaking
        if n < self._slot_size:
            slot[n:] = b"\x00" * (self._slot_size - n)

        # Release fence (simulated): make slot visible before advancing index
        self._write_idx = next_write
        return True

    def try_consume(self) -> bytes | None:
        """Attempt to consume one message from the ring buffer (consumer side).

        Returns None if the buffer is empty.

        Returns:
            Raw bytes from the slot, or None if empty.
        """
        if self._read_idx == self._write_idx:
            return None  # Empty

        slot = self._buffer[self._read_idx & self._mask]
        # Return a copy so consumer owns the data
        data = bytes(slot)

        # Acquire fence (simulated): read slot before advancing index
        self._read_idx = (self._read_idx + 1) & self._mask
        return data

    @property
    def size(self) -> int:
        """Current number of items in the buffer."""
        return (self._write_idx - self._read_idx) & self._mask

    @property
    def capacity(self) -> int:
        """Total slot capacity."""
        return self._capacity

    @property
    def is_empty(self) -> bool:
        """True if buffer has no pending messages."""
        return self._write_idx == self._read_idx

    @property
    def is_full(self) -> bool:
        """True if buffer cannot accept more messages."""
        return ((self._write_idx + 1) & self._mask) == self._read_idx


# ---------------------------------------------------------------------------
# Hot Path
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CPUAffinity:
    """Simulated CPU core pinning (isolcpus model).

    In production: uses sched_setaffinity / isolcpus kernel param
    to dedicate a core exclusively to the hot path thread.
    """

    core_id: int
    isolated: bool = True
    nohz_full: bool = True  # Tickless mode — no timer interrupts
    rcu_nocbs: bool = True  # Offload RCU callbacks to other cores


class HotPath:
    """Latency-critical order processing pipeline.

    Design principles (modeled, not enforced by Python runtime):
    - Pre-allocated buffer pool (Python operations still allocate)
    - No logging, no branching on the common case
    - SPSC ring model for hand-off to cold path
    - CPU pinning to isolated core (no context switches)
    - Kernel bypass NIC model (DPDK/ef_vi style)

    The `process` method simulates the minimal hot-path logic:
    receive raw wire bytes → validate → route → encode response → egress.
    """

    __slots__ = (
        "_buffer_pool",
        "_pool_size",
        "_buffer_size",
        "_pool_cursor",
        "_ring_to_cold",
        "_cpu_affinity",
        "_processed_count",
        "_total_process_ns",
    )

    def __init__(
        self,
        pool_size: int = 4096,
        buffer_size: int = 128,
        ring_capacity: int = 8192,
        pinned_core: int = 1,
    ) -> None:
        """Initialize hot path with pre-allocated resources.

        Args:
            pool_size: Number of pre-allocated buffers in the pool.
            buffer_size: Size of each buffer in bytes.
            ring_capacity: SPSC ring buffer capacity for cold-path hand-off.
            pinned_core: CPU core ID for affinity pinning.
        """
        # Pre-allocate buffer storage
        self._pool_size: int = pool_size
        self._buffer_size: int = buffer_size
        self._buffer_pool: list[bytearray] = [bytearray(buffer_size) for _ in range(pool_size)]
        self._pool_cursor: int = 0

        # Ring buffer to cold path (logging, risk, etc.)
        self._ring_to_cold: SPSCRingBuffer = SPSCRingBuffer(
            capacity=ring_capacity, slot_size=buffer_size
        )

        # CPU pinning model
        self._cpu_affinity: CPUAffinity = CPUAffinity(core_id=pinned_core)

        # Benchmarking
        self._processed_count: int = 0
        self._total_process_ns: int = 0

    def process(self, raw_bytes: bytes) -> bytes:
        """Process inbound wire bytes on the hot path.

        Illustrates a buffer-based processing path:
        1. Acquire pre-allocated buffer (round-robin pool)
        2. Copy inbound data (kernel-bypass NIC DMA model)
        3. Minimal validation (message type check only)
        4. Route to outbound buffer
        5. Hand off copy to cold path via the ring model

        Args:
            raw_bytes: Inbound SBE-encoded wire bytes.

        Returns:
            Outbound bytes (echo/ack pattern for simulation).

        Note:
            No hardware latency is established by this Python simulation.
        """
        t0 = time.perf_counter_ns()

        # Step 1: Acquire buffer from pre-allocated pool (no malloc)
        buf = self._buffer_pool[self._pool_cursor]
        self._pool_cursor = (self._pool_cursor + 1) % self._pool_size

        # Step 2: Copy into pre-allocated slot (simulates DMA scatter)
        n = min(len(raw_bytes), self._buffer_size)
        buf[:n] = raw_bytes[:n]

        # Step 3: Minimal validation — read message type from header only
        # No branching beyond type dispatch in real impl
        # (Omitted here: just pass through)

        # Step 4: Outbound is the same buffer (echo for simulation)
        outbound = bytes(buf[:n])

        # Step 5: Non-blocking hand-off to cold path
        # If ring is full, we DROP (never block hot path)
        self._ring_to_cold.try_publish(raw_bytes[:n])

        elapsed = time.perf_counter_ns() - t0
        self._total_process_ns += elapsed
        self._processed_count += 1

        return outbound

    @property
    def ring_to_cold(self) -> SPSCRingBuffer:
        """Access the ring buffer draining to cold path."""
        return self._ring_to_cold

    @property
    def avg_process_ns(self) -> float:
        """Average process() latency in nanoseconds."""
        return self._total_process_ns / max(self._processed_count, 1)

    @property
    def processed_count(self) -> int:
        """Total messages processed on hot path."""
        return self._processed_count

    @property
    def cpu_affinity(self) -> CPUAffinity:
        """CPU pinning configuration."""
        return self._cpu_affinity


# ---------------------------------------------------------------------------
# Cold Path
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PositionSnapshot:
    """Point-in-time position and P&L snapshot."""

    timestamp_ns: int
    symbol: bytes
    net_qty: int
    avg_price: int  # fixed-point
    unrealized_pnl: int  # fixed-point
    realized_pnl: int  # fixed-point


class ColdPath:
    """Non-latency-critical processing path.

    Handles:
    - Event logging (batched writes to reduce syscall overhead)
    - Risk reporting and position management
    - P&L calculation and periodic snapshots

    Runs on a separate thread/core from the hot path.
    Drains the SPSC ring buffer asynchronously.
    """

    __slots__ = (
        "_ring_from_hot",
        "_log_buffer",
        "_log_batch_size",
        "_positions",
        "_drain_thread",
        "_running",
        "_event_count",
        "_core_id",
    )

    def __init__(
        self,
        ring_from_hot: SPSCRingBuffer,
        log_batch_size: int = 64,
        core_id: int = 2,
    ) -> None:
        """Initialize cold path consumer.

        Args:
            ring_from_hot: SPSC ring buffer shared with hot path producer.
            log_batch_size: Number of events to batch before flushing.
            core_id: CPU core for cold path thread (separate from hot path).
        """
        self._ring_from_hot: SPSCRingBuffer = ring_from_hot
        self._log_buffer: list[bytes] = []
        self._log_batch_size: int = log_batch_size
        self._positions: dict[bytes, PositionSnapshot] = {}
        self._drain_thread: threading.Thread | None = None
        self._running: bool = False
        self._event_count: int = 0
        self._core_id: int = core_id

    def start(self) -> None:
        """Start the cold path drain thread."""
        self._running = True
        self._drain_thread = threading.Thread(
            target=self._drain_loop,
            name=f"cold-path-core{self._core_id}",
            daemon=True,
        )
        self._drain_thread.start()

    def stop(self) -> None:
        """Stop the cold path drain thread gracefully."""
        self._running = False
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=1.0)
            self._drain_thread = None
        # Flush remaining log buffer
        self._flush_logs()

    def _drain_loop(self) -> None:
        """Continuously drain ring buffer from hot path."""
        while self._running:
            msg = self._ring_from_hot.try_consume()
            if msg is not None:
                self._on_message(msg)
            else:
                # Yield CPU when idle — in production use pause/mwait
                time.sleep(0.000001)  # 1 µs idle spin

    def _on_message(self, raw: bytes) -> None:
        """Process a message drained from the hot path ring."""
        self._event_count += 1
        self.log_event(raw)

    def log_event(self, event: bytes) -> None:
        """Buffer an event for batched write.

        Events are accumulated until batch_size is reached,
        then flushed together to minimize I/O syscalls.

        Args:
            event: Raw event bytes to log.
        """
        self._log_buffer.append(event)
        if len(self._log_buffer) >= self._log_batch_size:
            self._flush_logs()

    def _flush_logs(self) -> None:
        """Flush accumulated log buffer.

        In production: write to memory-mapped file or send to
        log aggregation service (e.g., Kafka, Chronicle Queue).
        """
        if self._log_buffer:
            # Simulated flush — in production, mmap write or async I/O
            self._log_buffer.clear()

    def report_position(self, symbol: bytes | None = None) -> list[PositionSnapshot]:
        """Generate position snapshot(s).

        Args:
            symbol: Specific symbol to report, or None for all positions.

        Returns:
            List of current position snapshots.
        """
        if symbol is not None:
            snap = self._positions.get(symbol)
            return [snap] if snap else []
        return list(self._positions.values())

    def update_position(
        self,
        symbol: bytes,
        qty_delta: int,
        price: int,
        realized_pnl_delta: int = 0,
    ) -> None:
        """Update position tracking (called from cold path processing).

        Args:
            symbol: 8-byte symbol identifier.
            qty_delta: Signed quantity change (+buy, -sell).
            price: Execution price (fixed-point).
            realized_pnl_delta: Realized P&L increment (fixed-point).
        """
        now = time.time_ns()
        existing = self._positions.get(symbol)
        if existing is None:
            self._positions[symbol] = PositionSnapshot(
                timestamp_ns=now,
                symbol=symbol,
                net_qty=qty_delta,
                avg_price=price,
                unrealized_pnl=0,
                realized_pnl=realized_pnl_delta,
            )
        else:
            existing.net_qty += qty_delta
            existing.realized_pnl += realized_pnl_delta
            existing.timestamp_ns = now

    @property
    def event_count(self) -> int:
        """Total events processed on cold path."""
        return self._event_count

    @property
    def is_running(self) -> bool:
        """Whether the cold path drain thread is active."""
        return self._running


# ---------------------------------------------------------------------------
# Tick-to-Trade Latency Measurement
# ---------------------------------------------------------------------------


class TickToTradeStage(IntEnum):
    """Pipeline stages for tick-to-trade latency decomposition."""

    MARKET_DATA_RECV = 0
    SIGNAL_COMPUTE = 1
    ORDER_BUILD = 2
    RISK_CHECK = 3
    WIRE_ENCODE = 4
    NIC_EGRESS = 5


@dataclass(slots=True)
class TickToTradeResult:
    """Complete tick-to-trade measurement with per-stage breakdown.

    Attributes:
        event_id: Unique measurement identifier.
        total_ns: End-to-end latency in nanoseconds.
        stage_ns: Per-stage latency breakdown.
        meets_target: True if total_ns < 1000 (1 µs target).
    """

    event_id: int
    total_ns: int
    stage_ns: dict[str, int]
    meets_target: bool

    @property
    def total_us(self) -> float:
        """Total latency in microseconds."""
        return self.total_ns / 1_000.0


@dataclass(slots=True)
class _Measurement:
    """In-flight measurement state."""

    event_id: int
    start_ns: int
    checkpoints: dict[TickToTradeStage, int] = field(default_factory=dict)
    end_ns: int = 0


class TickToTrade:
    """End-to-end tick-to-trade latency measurement framework.

    Measures the full pipeline from market data arrival to order egress:
        MARKET_DATA_RECV → SIGNAL_COMPUTE → ORDER_BUILD →
        RISK_CHECK → WIRE_ENCODE → NIC_EGRESS

    Provides histogram and percentile calculations (p50, p99, p999)
    to characterize tail latency behavior.

    Target: < 1 microsecond (1000 ns) tick-to-trade.
    Reference: Jump Trading, Citadel Securities, Tower Research.
    """

    __slots__ = ("_in_flight", "_completed", "_latencies_ns")

    # Jump Trading / Citadel target
    TARGET_NS: int = 1_000  # 1 microsecond

    def __init__(self) -> None:
        self._in_flight: dict[int, _Measurement] = {}
        self._completed: list[TickToTradeResult] = []
        self._latencies_ns: list[int] = []

    def start_measurement(self, event_id: int) -> None:
        """Begin a tick-to-trade measurement.

        Call this at MARKET_DATA_RECV — the instant market data
        arrives from the NIC.

        Args:
            event_id: Unique identifier for this measurement.
        """
        now = time.perf_counter_ns()
        self._in_flight[event_id] = _Measurement(
            event_id=event_id,
            start_ns=now,
            checkpoints={TickToTradeStage.MARKET_DATA_RECV: now},
        )

    def checkpoint(self, event_id: int, stage: TickToTradeStage) -> None:
        """Record a stage checkpoint for an in-flight measurement.

        Args:
            event_id: Measurement identifier from start_measurement().
            stage: The pipeline stage being checkpointed.

        Raises:
            KeyError: If event_id has no active measurement.
        """
        m = self._in_flight.get(event_id)
        if m is None:
            raise KeyError(f"No active measurement for event_id={event_id}")
        m.checkpoints[stage] = time.perf_counter_ns()

    def complete(self, event_id: int) -> TickToTradeResult:
        """Complete a measurement and compute per-stage breakdown.

        Should be called at NIC_EGRESS — the instant the order
        bytes are handed to the NIC for transmission.

        Args:
            event_id: Measurement identifier from start_measurement().

        Returns:
            TickToTradeResult with full latency decomposition.

        Raises:
            KeyError: If event_id has no active measurement.
        """
        m = self._in_flight.pop(event_id, None)
        if m is None:
            raise KeyError(f"No active measurement for event_id={event_id}")

        end_ns = time.perf_counter_ns()
        m.end_ns = end_ns
        m.checkpoints[TickToTradeStage.NIC_EGRESS] = end_ns

        total_ns = end_ns - m.start_ns

        # Compute per-stage deltas
        stages = sorted(m.checkpoints.keys(), key=lambda s: s.value)
        stage_ns: dict[str, int] = {}
        for i in range(1, len(stages)):
            prev_stage = stages[i - 1]
            curr_stage = stages[i]
            delta = m.checkpoints[curr_stage] - m.checkpoints[prev_stage]
            stage_ns[curr_stage.name] = delta

        # Also include first stage (time-of-arrival baseline)
        if stages:
            stage_ns[stages[0].name] = 0

        result = TickToTradeResult(
            event_id=event_id,
            total_ns=total_ns,
            stage_ns=stage_ns,
            meets_target=total_ns < self.TARGET_NS,
        )

        self._completed.append(result)
        self._latencies_ns.append(total_ns)

        return result

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def percentile(self, p: float) -> float:
        """Compute latency percentile in nanoseconds.

        Args:
            p: Percentile value (0.0 to 100.0), e.g. 50, 99, 99.9.

        Returns:
            Latency at the given percentile in nanoseconds.

        Raises:
            ValueError: If no measurements have been completed.
        """
        if not self._latencies_ns:
            raise ValueError("No completed measurements to compute percentile")

        sorted_lat = sorted(self._latencies_ns)
        n = len(sorted_lat)
        # Nearest-rank method
        rank = max(0, min(n - 1, int((p / 100.0) * n)))
        return float(sorted_lat[rank])

    @property
    def p50_ns(self) -> float:
        """Median (50th percentile) latency in nanoseconds."""
        return self.percentile(50.0)

    @property
    def p99_ns(self) -> float:
        """99th percentile latency in nanoseconds."""
        return self.percentile(99.0)

    @property
    def p999_ns(self) -> float:
        """99.9th percentile latency in nanoseconds."""
        return self.percentile(99.9)

    @property
    def mean_ns(self) -> float:
        """Arithmetic mean latency in nanoseconds."""
        if not self._latencies_ns:
            return 0.0
        return sum(self._latencies_ns) / len(self._latencies_ns)

    @property
    def min_ns(self) -> int:
        """Minimum observed latency in nanoseconds."""
        return min(self._latencies_ns) if self._latencies_ns else 0

    @property
    def max_ns(self) -> int:
        """Maximum observed latency in nanoseconds."""
        return max(self._latencies_ns) if self._latencies_ns else 0

    @property
    def count(self) -> int:
        """Number of completed measurements."""
        return len(self._latencies_ns)

    @property
    def target_hit_rate(self) -> float:
        """Fraction of measurements meeting the < 1µs target."""
        if not self._completed:
            return 0.0
        hits = sum(1 for r in self._completed if r.meets_target)
        return hits / len(self._completed)

    def histogram(self, bucket_ns: int = 100) -> dict[int, int]:
        """Generate a latency histogram with fixed-width buckets.

        Args:
            bucket_ns: Bucket width in nanoseconds (default: 100ns).

        Returns:
            Dict mapping bucket lower-bound (ns) to count of measurements.
        """
        hist: dict[int, int] = {}
        for lat in self._latencies_ns:
            bucket = (lat // bucket_ns) * bucket_ns
            hist[bucket] = hist.get(bucket, 0) + 1
        return dict(sorted(hist.items()))

    def summary(self) -> str:
        """Human-readable latency summary string."""
        if not self._latencies_ns:
            return "TickToTrade: no measurements"
        return (
            f"TickToTrade: n={self.count} | "
            f"min={self.min_ns}ns | "
            f"p50={self.p50_ns:.0f}ns | "
            f"p99={self.p99_ns:.0f}ns | "
            f"p999={self.p999_ns:.0f}ns | "
            f"max={self.max_ns}ns | "
            f"target_hit={self.target_hit_rate:.1%}"
        )

    def reset(self) -> None:
        """Clear all measurements and statistics."""
        self._in_flight.clear()
        self._completed.clear()
        self._latencies_ns.clear()
