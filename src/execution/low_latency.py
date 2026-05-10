"""Python low-latency execution simulation.

This module intentionally models the shape of a production C++/kernel-bypass
order path while remaining deterministic and Pythonic for the lab:
risk gate -> venue submit -> match -> publish accounting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from chaos.benchmark import NS_PER_SECOND, percent_of_target, solace_target
from common.config import Settings
from common.models import Order, OrderStatus
from common.solace_client import InMemoryBroker, MessageBus
from exchange.matching_engine import MatchingEngine


class NanoClock(Protocol):
    def __call__(self) -> int:
        """Return monotonic nanoseconds."""


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Simple pre-trade limits used before the order reaches the matcher."""

    max_order_quantity: int = 1_000
    max_notional: Decimal = Decimal("1000000")
    allowed_symbols: frozenset[str] = frozenset({"FOO"})


@dataclass(frozen=True, slots=True)
class RiskDecision:
    accepted: bool
    reason: str | None = None


class RiskGate:
    """Deterministic pre-trade gate for quantity, notional, and symbol limits."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def check(self, order: Order) -> RiskDecision:
        if order.symbol not in self.limits.allowed_symbols:
            return RiskDecision(False, f"symbol {order.symbol} is not enabled")
        if order.quantity > self.limits.max_order_quantity:
            return RiskDecision(False, "quantity exceeds max_order_quantity")
        if order.price is not None:
            notional = order.price * Decimal(order.quantity)
            if notional > self.limits.max_notional:
                return RiskDecision(False, "notional exceeds max_notional")
        return RiskDecision(True)


@dataclass(frozen=True, slots=True)
class ExecutionTrace:
    """Timing and outcome for one execution-path submission."""

    order_id: str
    accepted_by_risk: bool
    venue_status: str
    fills: int
    risk_ns: int
    match_ns: int
    publish_ns: int
    total_ns: int
    reject_reason: str | None = None


class LowLatencyExecutor:
    """Single-threaded deterministic simulation of a low-latency order path."""

    def __init__(
        self,
        engine: MatchingEngine | None = None,
        *,
        bus: MessageBus | None = None,
        settings: Settings | None = None,
        risk_gate: RiskGate | None = None,
        clock: NanoClock | None = None,
    ) -> None:
        self.settings = (settings or Settings(in_memory_bus=True)).normalized()
        self.bus = bus or InMemoryBroker()
        self.clock = clock or time.perf_counter_ns
        self.engine = engine or MatchingEngine(self.bus, settings=self.settings, clock=self.clock)
        self.risk_gate = risk_gate or RiskGate()

    def submit(self, order: Order) -> ExecutionTrace:
        """Run one order through risk and matching, returning path timings."""

        start_ns = self.clock()
        decision = self.risk_gate.check(order)
        after_risk_ns = self.clock()
        if not decision.accepted:
            end_ns = self.clock()
            return ExecutionTrace(
                order_id=order.order_id,
                accepted_by_risk=False,
                venue_status=OrderStatus.REJECTED.value,
                fills=0,
                risk_ns=after_risk_ns - start_ns,
                match_ns=0,
                publish_ns=0,
                total_ns=end_ns - start_ns,
                reject_reason=decision.reason,
            )

        result = self.engine.process_order(order)
        after_match_ns = self.clock()
        after_publish_ns = self.clock()
        venue_status = result.acks[-1].status.value if result.acks else "UNKNOWN"
        return ExecutionTrace(
            order_id=order.order_id,
            accepted_by_risk=True,
            venue_status=venue_status,
            fills=result.fills_count,
            risk_ns=after_risk_ns - start_ns,
            match_ns=after_match_ns - after_risk_ns,
            publish_ns=after_publish_ns - after_match_ns,
            total_ns=after_publish_ns - start_ns,
        )


@dataclass(frozen=True, slots=True)
class ExecutionBenchmark:
    """Aggregate low-latency execution benchmark summary."""

    orders: int
    duration_ns: int
    achieved_msg_sec: float
    solace_target_msg_sec: int
    percent_of_solace_target: float
    rejected_by_risk: int


def benchmark_executor(
    traces: list[ExecutionTrace],
    *,
    message_size_bytes: int = 100,
) -> ExecutionBenchmark:
    """Compare an execution trace set to the Solace direct P2P software target."""

    if not traces:
        raise ValueError("traces must not be empty")
    duration_ns = max(1, sum(trace.total_ns for trace in traces))
    achieved = len(traces) / (duration_ns / NS_PER_SECOND)
    target = solace_target("direct", "p2p", message_size_bytes)
    return ExecutionBenchmark(
        orders=len(traces),
        duration_ns=duration_ns,
        achieved_msg_sec=achieved,
        solace_target_msg_sec=target.messages_per_second,
        percent_of_solace_target=percent_of_target(achieved, target.messages_per_second),
        rejected_by_risk=sum(1 for trace in traces if not trace.accepted_by_risk),
    )
