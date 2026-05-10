"""Deterministic performance benchmark tests."""

from __future__ import annotations

import csv
import os
from decimal import Decimal
from pathlib import Path

import pytest

from chaos.benchmark import (
    BenchmarkRunConfig,
    nearest_rank_percentile,
    percent_of_target,
    run_order_to_fill_benchmark,
    solace_target,
)
from common.config import Settings
from common.models import Order, Side
from common.solace_client import InMemoryBroker
from execution.low_latency import LowLatencyExecutor, RiskGate, RiskLimits


class StepClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 100
        return self.value


def test_solace_target_lookup_and_percent_of_target() -> None:
    direct = solace_target("direct", "p2p", 100)
    persistent_fanout = solace_target("persistent", "fanout", 1024, fanout=10)

    assert direct.table == "Table 2"
    assert direct.messages_per_second == 2_500_000
    assert persistent_fanout.table == "Table 5"
    assert persistent_fanout.messages_per_second == 820_000
    assert percent_of_target(2_000_000, direct.messages_per_second) == pytest.approx(0.8)


def test_percentile_math_is_deterministic_nearest_rank() -> None:
    values = list(range(1, 1001))

    assert nearest_rank_percentile(values, 50) == 500
    assert nearest_rank_percentile(values, 99) == 990
    assert nearest_rank_percentile(values, 99.9) == 999


def test_in_memory_benchmark_accounts_for_zero_loss_and_fanout() -> None:
    report = run_order_to_fill_benchmark(
        BenchmarkRunConfig(
            total_orders=20,
            target_orders_per_second=1_000_000,
            fanout=10,
            persistent=False,
            chaos_level=0,
        )
    )

    assert report.total_orders == 20
    assert report.fills == 10
    assert report.zero_loss
    assert report.lost_messages == 0
    assert report.egress_msg_sec > report.ingress_msg_sec


def test_persistent_ack_chaos_increases_tail_latency() -> None:
    direct = run_order_to_fill_benchmark(
        BenchmarkRunConfig(
            total_orders=100,
            target_orders_per_second=1_000_000,
            fanout=10,
            persistent=False,
            chaos_level=0,
        )
    )
    persistent = run_order_to_fill_benchmark(
        BenchmarkRunConfig(
            total_orders=100,
            target_orders_per_second=1_000_000,
            fanout=10,
            persistent=True,
            chaos_level=1.0,
        )
    )

    assert persistent.zero_loss
    assert persistent.p999_latency_ns > direct.p999_latency_ns
    assert persistent.p99_latency_ns > direct.p99_latency_ns


def test_low_latency_executor_rejects_risk_before_matching() -> None:
    bus = InMemoryBroker()
    executor = LowLatencyExecutor(
        bus=bus,
        settings=Settings(topic_prefix="test-lab", in_memory_bus=True),
        risk_gate=RiskGate(RiskLimits(max_order_quantity=1)),
        clock=StepClock(),
    )
    order = Order(
        order_id="too-large",
        symbol="FOO",
        side=Side.BUY,
        quantity=2,
        price=Decimal("100.00"),
        trader_id="risk-test",
        timestamp_ns=1,
    )

    trace = executor.submit(order)

    assert not trace.accepted_by_risk
    assert trace.venue_status == "REJECTED"
    assert trace.match_ns == 0
    assert trace.publish_ns == 0
    assert bus.published == []


@pytest.mark.skipif(
    os.getenv("STL_RUN_REAL_PERF") != "1",
    reason="real Solace/SDKPerf threshold gate is opt-in",
)
def test_real_sdkperf_results_reach_80_percent_when_enabled() -> None:
    csv_path = os.getenv("STL_PERF_RESULTS_CSV")
    if not csv_path:
        pytest.skip("set STL_PERF_RESULTS_CSV to artifacts/perf/<run_id>/sdkperf_results.csv")
    path = Path(csv_path)
    if not path.exists():
        pytest.skip(f"{path} does not exist")

    with path.open("r", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("status") == "ok"]
    if not rows:
        pytest.skip("no successful SDKPerf rows to gate")

    misses = [
        row["case_name"]
        for row in rows
        if float(row.get("percent_of_target") or 0) < 0.8
        or float(row.get("loss_percent") or 100) != 0
    ]
    assert misses == []
