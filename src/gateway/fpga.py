"""
FPGA Gateway Simulation — Sub-Microsecond Order Entry Infrastructure.

Explores configured hardware-style order entry paths. Simulates:

- Gate-level pipeline stages with deterministic latency
- Kernel-bypass networking (DPDK/ef_vi style)
- Hardware NIC timestamping at ingress/egress
- Pre-trade risk checks in FPGA fabric (sub-100ns)

This is a software timing model with assumed delays, not a validated hardware
implementation. It does not establish readiness for FPGA synthesis.

Architecture reference:
    Market Data NIC → FPGA Parse → Validate → Risk → Serialize → TX NIC
    Total tick-to-wire: ~150-400ns depending on configuration.
"""

from __future__ import annotations

import time
import random
import struct
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Protocol, Self


# =============================================================================
# Pipeline Stage Definitions
# =============================================================================


class PipelineStage(Enum):
    """Gate-level pipeline stages in the FPGA order path.

    Each stage represents a clock-domain boundary in the FPGA fabric.
    Stages execute sequentially in a single pipeline pass — no stalls
    under normal operation (back-pressure only on risk gate rejection).
    """

    PARSE = auto()       # Decode incoming market data / order struct
    VALIDATE = auto()    # Field validation, symbol lookup, enum checks
    RISK_CHECK = auto()  # Pre-trade risk: position, rate, fat-finger
    SERIALIZE = auto()   # Encode FIX/OUCH/ITCH wire format
    TRANSMIT = auto()    # DMA to NIC TX ring, hardware timestamp capture


class RiskCheckResult(Enum):
    """Outcome of the hardware risk gate evaluation."""

    PASS = auto()
    REJECT_POSITION_LIMIT = auto()
    REJECT_RATE_LIMIT = auto()
    REJECT_FAT_FINGER = auto()
    KILL_SWITCH_TRIGGERED = auto()


# =============================================================================
# Data Structures
# =============================================================================


@dataclass(slots=True, frozen=True)
class HardwareTimestamp:
    """NIC hardware timestamp captured at wire ingress/egress.

    Uses raw nanosecond counter from network adapter PTP clock.
    Resolution is typically 1ns on Mellanox ConnectX-6 / Solarflare X2522.
    """

    ns_since_epoch: int
    nic_port: int = 0
    is_ingress: bool = True

    @classmethod
    def now(cls, nic_port: int = 0, is_ingress: bool = True) -> Self:
        """Capture current time as a hardware timestamp."""
        return cls(
            ns_since_epoch=time.time_ns(),
            nic_port=nic_port,
            is_ingress=is_ingress,
        )


@dataclass(slots=True)
class PipelineLatency:
    """Per-stage latency measurement for a single order traversal."""

    stage: PipelineStage
    entry_ns: int
    exit_ns: int

    @property
    def duration_ns(self) -> int:
        return self.exit_ns - self.entry_ns


@dataclass(slots=True)
class OrderEntry:
    """Minimal order struct — cache-line aligned (64 bytes target).

    Layout mirrors what would be a packed C struct in FPGA firmware:
        [8B order_id][8B symbol_hash][8B price_raw][4B qty][1B side][1B type][34B pad]
    """

    order_id: int
    symbol: str
    price: float
    quantity: int
    side: str  # "BUY" | "SELL"
    order_type: str = "LIMIT"  # "LIMIT" | "MARKET" | "IOC"
    timestamp_ns: int = 0

    def __post_init__(self) -> None:
        if self.timestamp_ns == 0:
            self.timestamp_ns = time.time_ns()

    @property
    def wire_size_bytes(self) -> int:
        """Packed wire representation size (cache-line aligned)."""
        return 64  # Single cache line


@dataclass(slots=True)
class CancelEntry:
    """Fast-cancel message — minimal fields for speed."""

    order_id: int
    timestamp_ns: int = 0

    def __post_init__(self) -> None:
        if self.timestamp_ns == 0:
            self.timestamp_ns = time.time_ns()


@dataclass
class GatewayMetrics:
    """Aggregate gateway performance metrics.

    Tracks per-stage latency percentiles and overall tick-to-wire
    histogram for performance monitoring and regression detection.
    """

    # Per-stage latency samples (ns)
    stage_latencies: dict[PipelineStage, list[int]] = field(default_factory=dict)

    # Tick-to-wire total latency samples (ns)
    tick_to_wire_samples: list[int] = field(default_factory=list)

    # Throughput tracking
    orders_submitted: int = 0
    orders_rejected: int = 0
    cancels_submitted: int = 0
    peak_orders_per_second: float = 0.0

    # Sliding window for burst rate calculation
    _submission_times: deque[int] = field(default_factory=lambda: deque(maxlen=10000))

    def __post_init__(self) -> None:
        for stage in PipelineStage:
            if stage not in self.stage_latencies:
                self.stage_latencies[stage] = []

    def record_pipeline_pass(self, latencies: list[PipelineLatency], total_ns: int) -> None:
        """Record a complete pipeline traversal."""
        for lat in latencies:
            self.stage_latencies[lat.stage].append(lat.duration_ns)
        self.tick_to_wire_samples.append(total_ns)
        self.orders_submitted += 1

        now = time.time_ns()
        self._submission_times.append(now)
        self._update_peak_throughput()

    def record_rejection(self) -> None:
        """Record a risk gate rejection."""
        self.orders_rejected += 1

    def record_cancel(self) -> None:
        """Record a cancel submission."""
        self.cancels_submitted += 1

    def _update_peak_throughput(self) -> None:
        """Calculate peak burst throughput over sliding window."""
        if len(self._submission_times) < 2:
            return
        window_ns = self._submission_times[-1] - self._submission_times[0]
        if window_ns > 0:
            rate = (len(self._submission_times) - 1) / (window_ns / 1_000_000_000)
            self.peak_orders_per_second = max(self.peak_orders_per_second, rate)

    def percentile(self, stage: PipelineStage, p: float) -> int | None:
        """Get latency percentile for a pipeline stage.

        Args:
            stage: Pipeline stage to query.
            p: Percentile (0.0 to 1.0), e.g. 0.99 for p99.

        Returns:
            Latency in nanoseconds at the given percentile, or None if no data.
        """
        samples = self.stage_latencies.get(stage, [])
        if not samples:
            return None
        sorted_samples = sorted(samples)
        idx = int(len(sorted_samples) * p)
        idx = min(idx, len(sorted_samples) - 1)
        return sorted_samples[idx]

    def tick_to_wire_percentile(self, p: float) -> int | None:
        """Get tick-to-wire latency percentile.

        Args:
            p: Percentile (0.0 to 1.0).

        Returns:
            Total pipeline latency in nanoseconds, or None if no data.
        """
        if not self.tick_to_wire_samples:
            return None
        sorted_samples = sorted(self.tick_to_wire_samples)
        idx = int(len(sorted_samples) * p)
        idx = min(idx, len(sorted_samples) - 1)
        return sorted_samples[idx]

    def histogram(self, bucket_width_ns: int = 10) -> dict[int, int]:
        """Generate tick-to-wire latency histogram.

        Args:
            bucket_width_ns: Width of each histogram bucket in nanoseconds.

        Returns:
            Dict mapping bucket lower bound (ns) to count of samples.
        """
        hist: dict[int, int] = {}
        for sample in self.tick_to_wire_samples:
            bucket = (sample // bucket_width_ns) * bucket_width_ns
            hist[bucket] = hist.get(bucket, 0) + 1
        return dict(sorted(hist.items()))


# =============================================================================
# Risk Gate — Hardware Pre-Trade Risk
# =============================================================================


@dataclass(slots=True)
class RiskGate:
    """Pre-trade risk check implemented in FPGA fabric.

    Models hardware-accelerated risk validation that executes in sub-100ns
    for passing orders. In real FPGA implementations, this is a combinatorial
    logic block with registered outputs — no CPU involvement.

    Checks performed (in parallel in hardware):
        1. Position limit: net exposure per symbol
        2. Rate limit: orders/second throttle (prevent runaway algo)
        3. Fat-finger: notional value sanity check
        4. Kill switch: global halt on cumulative loss threshold

    Attributes:
        max_position_per_symbol: Maximum net shares per symbol.
        max_orders_per_second: Rate limiter threshold.
        max_notional_per_order: Fat-finger notional cap (price * qty).
        kill_switch_loss_threshold: Cumulative loss that triggers global halt.
        latency_ns: Typical pass-through latency for clean orders.
        kill_switch_active: Whether kill switch has been triggered.
    """

    max_position_per_symbol: int = 100_000
    max_orders_per_second: int = 50_000
    max_notional_per_order: float = 10_000_000.0
    kill_switch_loss_threshold: float = -5_000_000.0
    latency_ns: int = 65  # Sub-100ns for normal pass-through

    # Internal state (would be BRAM in real FPGA)
    _positions: dict[str, int] = field(default_factory=dict)
    _order_timestamps: deque[int] = field(default_factory=lambda: deque(maxlen=100_000))
    _cumulative_pnl: float = 0.0
    kill_switch_active: bool = False

    def check(self, order: OrderEntry) -> RiskCheckResult:
        """Evaluate pre-trade risk for an order.

        In hardware, all checks execute in parallel within a single clock cycle.
        The result is available after `latency_ns` propagation delay.

        Args:
            order: The order to validate.

        Returns:
            RiskCheckResult indicating pass or specific rejection reason.
        """
        # Kill switch overrides everything
        if self.kill_switch_active:
            return RiskCheckResult.KILL_SWITCH_TRIGGERED

        # Fat-finger check (combinatorial — no state lookup)
        notional = order.price * order.quantity
        if notional > self.max_notional_per_order:
            return RiskCheckResult.REJECT_FAT_FINGER

        # Position limit check (BRAM lookup by symbol hash)
        current_position = self._positions.get(order.symbol, 0)
        delta = order.quantity if order.side == "BUY" else -order.quantity
        new_position = current_position + delta
        if abs(new_position) > self.max_position_per_symbol:
            return RiskCheckResult.REJECT_POSITION_LIMIT

        # Rate limit check (counter comparison)
        now = time.time_ns()
        one_second_ago = now - 1_000_000_000
        # Count recent orders (in hardware: saturating counter with decay)
        recent_count = sum(1 for ts in self._order_timestamps if ts > one_second_ago)
        if recent_count >= self.max_orders_per_second:
            return RiskCheckResult.REJECT_RATE_LIMIT

        # All checks passed — update state
        self._positions[order.symbol] = new_position
        self._order_timestamps.append(now)
        return RiskCheckResult.PASS

    def update_pnl(self, realized_pnl_delta: float) -> None:
        """Update cumulative PnL and check kill switch.

        Args:
            realized_pnl_delta: Incremental PnL change.
        """
        self._cumulative_pnl += realized_pnl_delta
        if self._cumulative_pnl <= self.kill_switch_loss_threshold:
            self.kill_switch_active = True

    def reset_kill_switch(self) -> None:
        """Manual kill switch reset (requires human intervention in production)."""
        self.kill_switch_active = False

    def reset_positions(self) -> None:
        """Reset all position tracking (start-of-day)."""
        self._positions.clear()
        self._order_timestamps.clear()
        self._cumulative_pnl = 0.0
        self.kill_switch_active = False


# =============================================================================
# Kernel Bypass Networking
# =============================================================================


@dataclass(slots=True)
class HugepageBuffer:
    """Model of a 2MB hugepage-backed buffer region.

    In production, hugepages prevent TLB misses on the critical path.
    Buffers are pre-allocated at startup and never freed during trading.
    """

    page_size_bytes: int = 2 * 1024 * 1024  # 2MB hugepage
    num_pages: int = 512  # 1GB total
    _allocated: int = 0

    @property
    def total_bytes(self) -> int:
        return self.page_size_bytes * self.num_pages

    @property
    def bytes_available(self) -> int:
        return self.total_bytes - self._allocated

    def allocate(self, size_bytes: int) -> int:
        """Allocate from hugepage pool, returns offset.

        Args:
            size_bytes: Requested allocation size (rounded up to 64B alignment).

        Returns:
            Offset into the hugepage region.

        Raises:
            MemoryError: If insufficient hugepage memory available.
        """
        # Cache-line align all allocations
        aligned_size = ((size_bytes + 63) // 64) * 64
        if aligned_size > self.bytes_available:
            raise MemoryError(
                f"Hugepage pool exhausted: requested {aligned_size}B, "
                f"available {self.bytes_available}B"
            )
        offset = self._allocated
        self._allocated += aligned_size
        return offset


@dataclass(slots=True)
class RingBufferSlot:
    """Single slot in a lock-free ring buffer (cache-line sized)."""

    data: bytes = b""
    sequence: int = 0
    length: int = 0
    _padding: bytes = field(default=b"\x00" * 40, repr=False)  # Pad to 64B

    @property
    def cache_line_size(self) -> int:
        """Each slot occupies exactly one cache line."""
        return 64


class KernelBypass:
    """DPDK/ef_vi style kernel-bypass network stack simulation.

    Models zero-copy packet I/O that eliminates:
    - System call overhead (~1μs per syscall)
    - Kernel network stack processing (~2-5μs)
    - Memory copies between kernel/user space
    - Interrupt coalescing delays

    Instead uses:
    - Memory-mapped NIC ring buffers (DMA-accessible)
    - Busy-wait polling on RX completion queue
    - Hugepage-backed packet buffers (no TLB misses)
    - Batch processing for throughput (configurable trade-off)

    Attributes:
        ring_buffer_size: Number of slots in TX/RX rings (power of 2).
        batch_size: Maximum packets to process per poll iteration.
        busy_wait: If True, spin-poll; if False, use adaptive interrupt.
        poll_latency_ns: Modeled latency for a single poll iteration.
        hugepages: Pre-allocated hugepage memory pool.
    """

    __slots__ = (
        "ring_buffer_size",
        "batch_size",
        "busy_wait",
        "poll_latency_ns",
        "hugepages",
        "_tx_ring",
        "_rx_ring",
        "_tx_head",
        "_tx_tail",
        "_rx_head",
        "_rx_tail",
        "_packets_sent",
        "_packets_received",
        "_zero_copy_refs",
    )

    def __init__(
        self,
        ring_buffer_size: int = 4096,
        batch_size: int = 32,
        busy_wait: bool = True,
        poll_latency_ns: int = 50,
        hugepage_count: int = 512,
    ) -> None:
        """Initialize kernel-bypass network interface.

        Args:
            ring_buffer_size: Ring buffer depth (must be power of 2).
            batch_size: Max packets per batch send/recv.
            busy_wait: True for busy-poll, False for interrupt-driven.
            poll_latency_ns: Simulated poll loop iteration time.
            hugepage_count: Number of 2MB hugepages to pre-allocate.

        Raises:
            ValueError: If ring_buffer_size is not a power of 2.
        """
        if ring_buffer_size & (ring_buffer_size - 1) != 0:
            raise ValueError(
                f"ring_buffer_size must be power of 2, got {ring_buffer_size}"
            )

        self.ring_buffer_size = ring_buffer_size
        self.batch_size = batch_size
        self.busy_wait = busy_wait
        self.poll_latency_ns = poll_latency_ns
        self.hugepages = HugepageBuffer(num_pages=hugepage_count)

        # Pre-allocate ring buffers
        self._tx_ring: list[RingBufferSlot] = [
            RingBufferSlot() for _ in range(ring_buffer_size)
        ]
        self._rx_ring: list[RingBufferSlot] = [
            RingBufferSlot() for _ in range(ring_buffer_size)
        ]

        # Ring buffer pointers (atomic in real implementation)
        self._tx_head: int = 0
        self._tx_tail: int = 0
        self._rx_head: int = 0
        self._rx_tail: int = 0

        # Statistics
        self._packets_sent: int = 0
        self._packets_received: int = 0

        # Zero-copy reference tracking (prevent premature buffer reuse)
        self._zero_copy_refs: dict[int, int] = {}  # offset -> refcount

    def send(self, payload_bytes: bytes) -> int:
        """Transmit payload via zero-copy DMA to NIC TX ring.

        Zero-copy semantics: the payload buffer is *not* copied — a descriptor
        pointing to the hugepage-backed buffer is placed in the TX ring.
        The NIC DMA engine reads directly from the buffer.

        In this simulation, we model the timing but do copy for correctness.

        Args:
            payload_bytes: Raw wire-format payload (max 1500B for standard MTU).

        Returns:
            Nanosecond timestamp when packet was submitted to TX ring.

        Raises:
            BufferError: If TX ring is full (back-pressure from NIC).
        """
        # Check ring space
        next_head = (self._tx_head + 1) & (self.ring_buffer_size - 1)
        if next_head == self._tx_tail:
            raise BufferError(
                "TX ring full — NIC back-pressure. "
                f"Head={self._tx_head}, Tail={self._tx_tail}"
            )

        # Zero-copy: write descriptor only (no memcpy in real implementation)
        # In hardware: DMA descriptor points to hugepage buffer offset
        slot = self._tx_ring[self._tx_head]
        slot.data = payload_bytes  # Simulation: actual copy for correctness
        slot.length = len(payload_bytes)
        slot.sequence = self._packets_sent

        # Advance head pointer (memory barrier in real code: __atomic_store)
        self._tx_head = next_head
        self._packets_sent += 1

        # Return simulated wire timestamp
        return time.time_ns()

    def recv(self) -> tuple[bytes | None, int]:
        """Poll RX ring for incoming packets.

        Implements busy-wait or adaptive interrupt polling:
        - Busy-wait: spin on RX tail pointer (lowest latency, highest CPU)
        - Interrupt: sleep until NIC signals completion (saves CPU, adds ~1μs)

        Returns:
            Tuple of (payload_bytes or None if empty, poll_timestamp_ns).
        """
        poll_ts = time.time_ns()

        # Check if data available
        if self._rx_head == self._rx_tail:
            if not self.busy_wait:
                # Simulate interrupt delay (adaptive coalescing)
                pass  # In real code: epoll_wait or eventfd
            return None, poll_ts

        # Consume from RX ring
        slot = self._rx_ring[self._rx_tail]
        payload = slot.data
        self._rx_tail = (self._rx_tail + 1) & (self.ring_buffer_size - 1)
        self._packets_received += 1

        return payload, poll_ts

    def inject_rx(self, payload_bytes: bytes) -> None:
        """Inject a packet into the RX ring (simulates NIC DMA write).

        Used in simulation to model incoming market data.

        Args:
            payload_bytes: Incoming packet payload.

        Raises:
            BufferError: If RX ring is full (packets would be dropped).
        """
        next_head = (self._rx_head + 1) & (self.ring_buffer_size - 1)
        if next_head == self._rx_tail:
            raise BufferError("RX ring full — packet drop")

        slot = self._rx_ring[self._rx_head]
        slot.data = payload_bytes
        slot.length = len(payload_bytes)
        slot.sequence = self._packets_received

        self._rx_head = next_head

    @property
    def tx_ring_utilization(self) -> float:
        """Fraction of TX ring slots in use."""
        used = (self._tx_head - self._tx_tail) & (self.ring_buffer_size - 1)
        return used / self.ring_buffer_size

    @property
    def rx_ring_utilization(self) -> float:
        """Fraction of RX ring slots in use."""
        used = (self._rx_head - self._rx_tail) & (self.ring_buffer_size - 1)
        return used / self.ring_buffer_size


# =============================================================================
# FPGA Gateway — Main Order Entry Pipeline
# =============================================================================


class FPGAGateway:
    """FPGA-accelerated order entry gateway simulation.

    Models a hardware trading gateway that bypasses the operating system
    kernel entirely. The order path is implemented as a fixed-function
    pipeline in FPGA fabric (Xilinx Alveo U250 / Intel Stratix 10 class).

    Pipeline architecture:
        NIC RX → PARSE → VALIDATE → RISK_CHECK → SERIALIZE → NIC TX
        |←——————— ~150-350ns typical ————————————→|

    Key properties:
        - Deterministic latency (no OS jitter, no GC pauses)
        - Wire-speed processing (one order per clock cycle throughput)
        - Hardware timestamps at NIC ingress/egress (1ns resolution)
        - Separate fast-cancel path (~30ns, bypasses serialize)

    Attributes:
        wire_latency_ns: Physical wire propagation + PHY latency.
        logic_latency_ns: Per-stage FPGA logic latency.
        variance_ns: Maximum random jitter per stage (models routing delay).
        risk_gate: Hardware risk check module.
        metrics: Performance tracking.
        nic: Kernel-bypass network interface.
    """

    __slots__ = (
        "wire_latency_ns",
        "logic_latency_ns",
        "variance_ns",
        "cancel_latency_ns",
        "risk_gate",
        "metrics",
        "nic",
        "_pipeline_active",
        "_stage_latencies",
    )

    # Default per-stage latency configuration (nanoseconds)
    DEFAULT_STAGE_LATENCIES: dict[PipelineStage, int] = {
        PipelineStage.PARSE: 35,        # Protocol decode (FIX/OUCH/ITCH)
        PipelineStage.VALIDATE: 25,     # Field validation, symbol lookup
        PipelineStage.RISK_CHECK: 65,   # Position + rate + fat-finger
        PipelineStage.SERIALIZE: 30,    # Wire format encode
        PipelineStage.TRANSMIT: 20,     # DMA to NIC TX descriptor ring
    }

    def __init__(
        self,
        wire_latency_ns: int = 85,
        logic_latency_ns: int = 175,
        variance_ns: int = 5,
        cancel_latency_ns: int = 30,
        risk_gate: RiskGate | None = None,
        nic: KernelBypass | None = None,
        stage_latencies: dict[PipelineStage, int] | None = None,
    ) -> None:
        """Initialize FPGA gateway with configurable timing model.

        Args:
            wire_latency_ns: NIC PHY + cable propagation delay (each way).
            logic_latency_ns: Total FPGA logic pipeline depth (sum of stages).
            variance_ns: Max per-stage jitter (±variance_ns/2).
            cancel_latency_ns: Fast-cancel path latency (bypasses full pipeline).
            risk_gate: Pre-trade risk module (created with defaults if None).
            nic: Kernel-bypass NIC interface (created with defaults if None).
            stage_latencies: Override per-stage latency configuration.
        """
        self.wire_latency_ns = wire_latency_ns
        self.logic_latency_ns = logic_latency_ns
        self.variance_ns = variance_ns
        self.cancel_latency_ns = cancel_latency_ns
        self.risk_gate = risk_gate or RiskGate()
        self.metrics = GatewayMetrics()
        self.nic = nic or KernelBypass()
        self._pipeline_active = True

        # Configure per-stage latencies
        self._stage_latencies = stage_latencies or self.DEFAULT_STAGE_LATENCIES.copy()

    def submit_order(
        self, order: OrderEntry
    ) -> tuple[OrderEntry, HardwareTimestamp, HardwareTimestamp]:
        """Submit an order through the FPGA pipeline.

        Processes the order through all pipeline stages with deterministic
        latency. Hardware timestamps are captured at NIC ingress (entry)
        and NIC egress (wire).

        Pipeline: PARSE → VALIDATE → RISK_CHECK → SERIALIZE → TRANSMIT

        Args:
            order: Order to submit.

        Returns:
            Tuple of (order, entry_timestamp, wire_timestamp).

        Raises:
            RuntimeError: If pipeline is halted (kill switch active).
            ValueError: If order fails validation stage.
        """
        if not self._pipeline_active:
            raise RuntimeError("FPGA pipeline halted — kill switch active")

        # NIC ingress hardware timestamp
        entry_timestamp = HardwareTimestamp.now(nic_port=0, is_ingress=True)
        pipeline_start_ns = entry_timestamp.ns_since_epoch

        # Execute pipeline stages
        stage_measurements: list[PipelineLatency] = []
        current_ns = pipeline_start_ns

        for stage in PipelineStage:
            stage_start = current_ns
            stage_duration = self._compute_stage_latency(stage)

            # Risk check stage has special handling
            if stage == PipelineStage.RISK_CHECK:
                result = self.risk_gate.check(order)
                if result != RiskCheckResult.PASS:
                    self.metrics.record_rejection()
                    if result == RiskCheckResult.KILL_SWITCH_TRIGGERED:
                        self._pipeline_active = False
                    raise ValueError(
                        f"Order rejected at risk gate: {result.name} "
                        f"(order_id={order.order_id}, symbol={order.symbol})"
                    )

            current_ns += stage_duration
            stage_measurements.append(
                PipelineLatency(stage=stage, entry_ns=stage_start, exit_ns=current_ns)
            )

        # Add wire latency for TX path
        wire_ns = current_ns + self.wire_latency_ns

        # NIC egress hardware timestamp
        wire_timestamp = HardwareTimestamp(
            ns_since_epoch=wire_ns, nic_port=0, is_ingress=False
        )

        # Serialize and send via kernel-bypass NIC
        payload = self._serialize_order(order)
        self.nic.send(payload)

        # Record metrics
        total_pipeline_ns = wire_ns - pipeline_start_ns
        self.metrics.record_pipeline_pass(stage_measurements, total_pipeline_ns)

        return order, entry_timestamp, wire_timestamp

    def cancel_order(
        self, order_id: int
    ) -> tuple[CancelEntry, HardwareTimestamp, HardwareTimestamp]:
        """Submit a cancel via the fast-cancel hardware path.

        The fast-cancel path bypasses SERIALIZE stage — uses a pre-formatted
        cancel template with only the order_id field substituted. This achieves
        ~30ns cancel latency vs ~175ns for full order pipeline.

        In hardware: single-cycle BRAM lookup + fixed template XOR.

        Args:
            order_id: ID of the order to cancel.

        Returns:
            Tuple of (cancel_entry, entry_timestamp, wire_timestamp).

        Raises:
            RuntimeError: If pipeline is halted.
        """
        if not self._pipeline_active:
            raise RuntimeError("FPGA pipeline halted — kill switch active")

        cancel = CancelEntry(order_id=order_id)

        # NIC ingress timestamp
        entry_timestamp = HardwareTimestamp.now(nic_port=0, is_ingress=True)

        # Fast-cancel path: reduced pipeline (PARSE → TRANSMIT only)
        jitter = random.randint(0, self.variance_ns) if self.variance_ns > 0 else 0
        cancel_duration_ns = self.cancel_latency_ns + jitter

        # Wire timestamp
        wire_ns = entry_timestamp.ns_since_epoch + cancel_duration_ns + self.wire_latency_ns
        wire_timestamp = HardwareTimestamp(
            ns_since_epoch=wire_ns, nic_port=0, is_ingress=False
        )

        # Send cancel payload
        cancel_payload = struct.pack("<Q", order_id)  # 8-byte cancel message
        self.nic.send(cancel_payload)

        self.metrics.record_cancel()
        return cancel, entry_timestamp, wire_timestamp

    def _compute_stage_latency(self, stage: PipelineStage) -> int:
        """Compute latency for a pipeline stage with optional jitter.

        Models deterministic FPGA timing with small variance from
        routing congestion and temperature-dependent gate delays.

        Args:
            stage: Pipeline stage to compute latency for.

        Returns:
            Latency in nanoseconds.
        """
        base_latency = self._stage_latencies.get(
            stage, self.logic_latency_ns // len(PipelineStage)
        )
        if self.variance_ns > 0:
            jitter = random.randint(-self.variance_ns // 2, self.variance_ns // 2)
            return max(1, base_latency + jitter)
        return base_latency

    def _serialize_order(self, order: OrderEntry) -> bytes:
        """Serialize order to wire format (OUCH-like binary protocol).

        In real FPGA: this is hardwired combinatorial logic, not code.
        Output is cache-line aligned (64 bytes) for DMA efficiency.

        Args:
            order: Order to serialize.

        Returns:
            64-byte wire-format payload.
        """
        # Pack into fixed 64-byte frame
        # [8B order_id][8B symbol_hash][8B price_fixed][4B qty][1B side][1B type][34B zero]
        symbol_hash = hash(order.symbol) & 0xFFFFFFFFFFFFFFFF
        price_fixed = int(order.price * 1_000_000)  # 6 decimal fixed-point
        side_byte = 0x42 if order.side == "BUY" else 0x53  # 'B' or 'S'
        type_map = {"LIMIT": 0x01, "MARKET": 0x02, "IOC": 0x03}
        type_byte = type_map.get(order.order_type, 0x01)

        payload = struct.pack(
            "<QQqIBB",
            order.order_id,
            symbol_hash,
            price_fixed,
            order.quantity,
            side_byte,
            type_byte,
        )
        # Pad to 64 bytes (cache-line aligned)
        payload = payload.ljust(64, b"\x00")
        return payload

    @property
    def total_pipeline_latency_ns(self) -> int:
        """Sum of all stage latencies (no jitter)."""
        return sum(self._stage_latencies.values())

    @property
    def estimated_tick_to_wire_ns(self) -> int:
        """Estimated total tick-to-wire including wire latency."""
        return self.total_pipeline_latency_ns + self.wire_latency_ns

    def halt(self) -> None:
        """Emergency halt — disable pipeline immediately."""
        self._pipeline_active = False

    def resume(self) -> None:
        """Resume pipeline after halt (requires risk gate reset)."""
        if self.risk_gate.kill_switch_active:
            raise RuntimeError(
                "Cannot resume: kill switch still active. "
                "Call risk_gate.reset_kill_switch() first."
            )
        self._pipeline_active = True


# =============================================================================
# Factory / Convenience
# =============================================================================


def create_gateway(
    profile: str = "aggressive",
) -> FPGAGateway:
    """Create a pre-configured FPGA gateway for common use cases.

    Profiles:
        - "aggressive": Minimum latency, tight risk limits (prop trading)
        - "balanced": Moderate latency, standard risk (market making)
        - "conservative": Higher latency, wide risk limits (execution algo)

    Args:
        profile: Configuration profile name.

    Returns:
        Configured FPGAGateway instance.

    Raises:
        ValueError: If profile name is not recognized.
    """
    profiles: dict[str, dict[str, Any]] = {
        "aggressive": {
            "wire_latency_ns": 65,
            "logic_latency_ns": 150,
            "variance_ns": 2,
            "cancel_latency_ns": 25,
            "risk_gate": RiskGate(
                max_position_per_symbol=50_000,
                max_orders_per_second=100_000,
                max_notional_per_order=5_000_000.0,
                kill_switch_loss_threshold=-2_000_000.0,
                latency_ns=50,
            ),
        },
        "balanced": {
            "wire_latency_ns": 85,
            "logic_latency_ns": 200,
            "variance_ns": 5,
            "cancel_latency_ns": 35,
            "risk_gate": RiskGate(
                max_position_per_symbol=100_000,
                max_orders_per_second=50_000,
                max_notional_per_order=10_000_000.0,
                kill_switch_loss_threshold=-5_000_000.0,
                latency_ns= 65,
            ),
        },
        "conservative": {
            "wire_latency_ns": 120,
            "logic_latency_ns": 300,
            "variance_ns": 10,
            "cancel_latency_ns": 50,
            "risk_gate": RiskGate(
                max_position_per_symbol=200_000,
                max_orders_per_second=20_000,
                max_notional_per_order=25_000_000.0,
                kill_switch_loss_threshold=-10_000_000.0,
                latency_ns= 80,
            ),
        },
    }

    if profile not in profiles:
        raise ValueError(
            f"Unknown profile '{profile}'. Available: {list(profiles.keys())}"
        )

    return FPGAGateway(**profiles[profile])
