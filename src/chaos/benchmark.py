"""Deterministic performance and chaos benchmark helpers.

The real Solace broker/SDKPerf runs are intentionally opt-in because absolute
throughput depends on host CPU, NIC, TLS, Docker, and storage. The functions in
this module keep the benchmark math, loss accounting, and tail-latency
simulation deterministic enough for CI while still exporting the same artifact
shapes used by the real benchmark script.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx

from common.config import Settings
from common.models import Order, Side
from common.solace_client import InMemoryBroker
from exchange.matching_engine import MatchingEngine

DeliveryMode = Literal["direct", "persistent"]
TrafficPattern = Literal["p2p", "fanout"]

NS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class SolaceTarget:
    """One Solace software ESXi target from the public performance tables."""

    table: str
    delivery_mode: DeliveryMode
    pattern: TrafficPattern
    message_size_bytes: int
    fanout: int
    messages_per_second: int
    notes: str = ""

    @property
    def key(self) -> str:
        return (
            f"{self.delivery_mode}:{self.pattern}:"
            f"{self.message_size_bytes}B:f{self.fanout}"
        )


SOLACE_SOFTWARE_ESXI_TARGETS: tuple[SolaceTarget, ...] = (
    SolaceTarget("Table 2", "direct", "p2p", 100, 1, 2_500_000),
    SolaceTarget("Table 2", "direct", "p2p", 1_024, 1, 410_000),
    SolaceTarget("Table 2", "direct", "p2p", 2_048, 1, 260_000),
    SolaceTarget("Table 2", "direct", "p2p", 10_240, 1, 91_000),
    SolaceTarget("Table 2", "direct", "p2p", 20_480, 1, 56_000),
    SolaceTarget("Table 3", "direct", "fanout", 100, 1, 2_500_000),
    SolaceTarget("Table 3", "direct", "fanout", 100, 2, 4_000_000),
    SolaceTarget("Table 3", "direct", "fanout", 100, 5, 7_400_000),
    SolaceTarget("Table 3", "direct", "fanout", 100, 10, 8_800_000),
    SolaceTarget("Table 3", "direct", "fanout", 1_024, 1, 410_000),
    SolaceTarget("Table 3", "direct", "fanout", 1_024, 2, 660_000),
    SolaceTarget("Table 3", "direct", "fanout", 1_024, 5, 1_000_000, "NIC limit"),
    SolaceTarget("Table 3", "direct", "fanout", 1_024, 10, 1_000_000, "NIC limit"),
    SolaceTarget("Table 3", "direct", "fanout", 2_048, 1, 260_000),
    SolaceTarget("Table 3", "direct", "fanout", 2_048, 2, 390_000),
    SolaceTarget("Table 3", "direct", "fanout", 2_048, 5, 640_000, "NIC limit"),
    SolaceTarget("Table 3", "direct", "fanout", 2_048, 10, 640_000, "NIC limit"),
    SolaceTarget("Table 4", "persistent", "p2p", 512, 1, 200_000),
    SolaceTarget("Table 4", "persistent", "p2p", 1_024, 1, 170_000),
    SolaceTarget("Table 4", "persistent", "p2p", 2_048, 1, 130_000),
    SolaceTarget("Table 4", "persistent", "p2p", 4_096, 1, 92_000),
    SolaceTarget("Table 4", "persistent", "p2p", 102_400, 1, 3_000),
    SolaceTarget("Table 5", "persistent", "fanout", 1_024, 1, 170_000),
    SolaceTarget("Table 5", "persistent", "fanout", 1_024, 2, 300_000),
    SolaceTarget("Table 5", "persistent", "fanout", 1_024, 5, 580_000),
    SolaceTarget("Table 5", "persistent", "fanout", 1_024, 10, 820_000),
    SolaceTarget("Table 5", "persistent", "fanout", 2_048, 1, 130_000),
    SolaceTarget("Table 5", "persistent", "fanout", 2_048, 2, 220_000),
    SolaceTarget("Table 5", "persistent", "fanout", 2_048, 5, 410_000),
    SolaceTarget("Table 5", "persistent", "fanout", 2_048, 10, 450_000),
    SolaceTarget("Table 5", "persistent", "fanout", 4_096, 1, 90_000),
    SolaceTarget("Table 5", "persistent", "fanout", 4_096, 2, 150_000),
    SolaceTarget("Table 5", "persistent", "fanout", 4_096, 5, 220_000),
    SolaceTarget("Table 5", "persistent", "fanout", 4_096, 10, 240_000),
    SolaceTarget("Table 5", "persistent", "fanout", 102_400, 1, 3_000),
    SolaceTarget("Table 5", "persistent", "fanout", 102_400, 2, 7_000),
    SolaceTarget("Table 5", "persistent", "fanout", 102_400, 5, 9_000),
    SolaceTarget("Table 5", "persistent", "fanout", 102_400, 10, 10_000),
)


def solace_target(
    delivery_mode: DeliveryMode,
    pattern: TrafficPattern,
    message_size_bytes: int,
    *,
    fanout: int = 1,
) -> SolaceTarget:
    """Return the exact software ESXi target for one benchmark case."""

    for target in SOLACE_SOFTWARE_ESXI_TARGETS:
        if (
            target.delivery_mode == delivery_mode
            and target.pattern == pattern
            and target.message_size_bytes == message_size_bytes
            and target.fanout == fanout
        ):
            return target
    raise KeyError(
        "no Solace software ESXi target for "
        f"{delivery_mode}/{pattern}/{message_size_bytes}B/fanout={fanout}"
    )


def percent_of_target(
    actual_messages_per_second: float,
    target_messages_per_second: float,
) -> float:
    """Return actual throughput as a fraction of the vendor target."""

    if target_messages_per_second <= 0:
        raise ValueError("target_messages_per_second must be positive")
    return actual_messages_per_second / target_messages_per_second


def nearest_rank_percentile(values: Sequence[int | float], percentile: float) -> float:
    """Compute a deterministic nearest-rank percentile."""

    if not values:
        raise ValueError("cannot compute percentile for an empty sample")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    rank = math.ceil((Decimal(str(percentile)) / Decimal("100")) * Decimal(len(ordered)))
    index = max(0, int(rank) - 1)
    return float(ordered[index])


@dataclass(slots=True)
class DeterministicBenchmarkClock:
    """Monotonic nanosecond clock that advances only when explicitly asked."""

    current_ns: int = 0
    tick_ns: int = 100

    def now_ns(self) -> int:
        self.current_ns += self.tick_ns
        return self.current_ns

    def advance_ns(self, amount_ns: int) -> int:
        if amount_ns < 0:
            raise ValueError("amount_ns must be non-negative")
        self.current_ns += amount_ns
        return self.current_ns

    def advance_to(self, timestamp_ns: int) -> int:
        if timestamp_ns > self.current_ns:
            self.current_ns = timestamp_ns
        return self.current_ns


@dataclass(frozen=True, slots=True)
class PersistentAckDelayModel:
    """Deterministic approximation of store-and-ACK latency under IOPS pressure."""

    baseline_iops: int = 170_000
    base_overhead_ns: int = 25_000
    jitter_ns: int = 75_000

    def delay_ns(self, sequence: int, *, chaos_level: float, load_fraction: float) -> int:
        if self.baseline_iops <= 0:
            raise ValueError("baseline_iops must be positive")
        if chaos_level < 0:
            raise ValueError("chaos_level must be non-negative")
        pressure = max(0.0, load_fraction - 1.0)
        fsync_floor = math.ceil(NS_PER_SECOND / self.baseline_iops)
        deterministic_jitter = (sequence * 7_919) % max(1, self.jitter_ns)
        multiplier = 1.0 + chaos_level + pressure
        return int((self.base_overhead_ns + fsync_floor + deterministic_jitter) * multiplier)


@dataclass(frozen=True, slots=True)
class BenchmarkRunConfig:
    """Input knobs for deterministic order-to-fill latency experiments."""

    total_orders: int = 1_000
    target_orders_per_second: int = 1_000_000
    fanout: int = 10
    persistent: bool = False
    chaos_level: float = 0.0
    symbol: str = "FOO"
    topic_prefix: str = "perf-lab"

    def __post_init__(self) -> None:
        if self.total_orders < 2:
            raise ValueError("total_orders must be at least 2")
        if self.target_orders_per_second <= 0:
            raise ValueError("target_orders_per_second must be positive")
        if self.fanout <= 0:
            raise ValueError("fanout must be positive")
        if self.chaos_level < 0:
            raise ValueError("chaos_level must be non-negative")


@dataclass(frozen=True, slots=True)
class LatencyBenchmarkReport:
    """Aggregate results for a deterministic latency run."""

    name: str
    total_orders: int
    fills: int
    fanout: int
    target_orders_per_second: int
    persistent: bool
    chaos_level: float
    duration_ns: int
    ingress_msg_sec: float
    egress_msg_sec: float
    p50_latency_ns: float
    p99_latency_ns: float
    p999_latency_ns: float
    max_latency_ns: int
    lost_messages: int

    @property
    def zero_loss(self) -> bool:
        return self.lost_messages == 0

    def to_row(self) -> dict[str, object]:
        row = asdict(self)
        row["zero_loss"] = self.zero_loss
        return row


def run_order_to_fill_benchmark(
    config: BenchmarkRunConfig,
    *,
    ack_delay_model: PersistentAckDelayModel | None = None,
) -> LatencyBenchmarkReport:
    """Run a deterministic replay-like order burst through the matching engine."""

    clock = DeterministicBenchmarkClock(current_ns=1_000_000, tick_ns=100)
    bus = InMemoryBroker()
    settings = Settings(topic_prefix=config.topic_prefix, in_memory_bus=True, symbol=config.symbol)
    engine = MatchingEngine(bus, settings=settings, clock=clock.now_ns)
    ack_model = ack_delay_model or PersistentAckDelayModel()
    inter_arrival_ns = max(1, NS_PER_SECOND // config.target_orders_per_second)
    direct_target = solace_target("direct", "fanout", 100, fanout=min(config.fanout, 10))
    load_fraction = config.target_orders_per_second / direct_target.messages_per_second
    latencies: list[int] = []
    start_ns = clock.current_ns

    for index in range(config.total_orders):
        send_ns = start_ns + index * inter_arrival_ns
        clock.advance_to(send_ns)
        if config.persistent:
            clock.advance_ns(
                ack_model.delay_ns(
                    index + 1,
                    chaos_level=config.chaos_level,
                    load_fraction=load_fraction,
                )
            )
        elif config.chaos_level:
            clock.advance_ns(int(config.chaos_level * 1_000))

        side = Side.SELL if index % 2 == 0 else Side.BUY
        order = Order(
            order_id=f"perf-{index:012d}",
            symbol=config.symbol,
            side=side,
            quantity=1,
            price=Decimal("100.00"),
            trader_id=f"strategy-{index % config.fanout:02d}",
            timestamp_ns=send_ns,
        )
        result = engine.process_order(order)
        if result.fills_count:
            fanout_ns = config.fanout * 50
            clock.advance_ns(fanout_ns)
            latencies.append(clock.current_ns - send_ns)

    duration_ns = max(1, clock.current_ns - start_ns)
    expected_fills = config.total_orders // 2
    lost_messages = max(0, expected_fills - len(latencies))
    ingress_msg_sec = config.total_orders / (duration_ns / NS_PER_SECOND)
    egress_msg_sec = len(latencies) * config.fanout / (duration_ns / NS_PER_SECOND)
    name = (
        f"{'persistent' if config.persistent else 'direct'}-"
        f"f{config.fanout}-c{config.chaos_level:g}-r{config.target_orders_per_second}"
    )

    return LatencyBenchmarkReport(
        name=name,
        total_orders=config.total_orders,
        fills=len(latencies),
        fanout=config.fanout,
        target_orders_per_second=config.target_orders_per_second,
        persistent=config.persistent,
        chaos_level=config.chaos_level,
        duration_ns=duration_ns,
        ingress_msg_sec=ingress_msg_sec,
        egress_msg_sec=egress_msg_sec,
        p50_latency_ns=nearest_rank_percentile(latencies, 50),
        p99_latency_ns=nearest_rank_percentile(latencies, 99),
        p999_latency_ns=nearest_rank_percentile(latencies, 99.9),
        max_latency_ns=int(max(latencies)),
        lost_messages=lost_messages,
    )


def write_reports_csv(reports: Iterable[LatencyBenchmarkReport], path: Path) -> None:
    """Write latency benchmark reports to CSV."""

    rows = [report.to_row() for report in reports]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_prometheus_metrics(report: LatencyBenchmarkReport, path: Path) -> None:
    """Write Prometheus text exposition for one report."""

    try:
        from prometheus_client import CollectorRegistry, Gauge, generate_latest
    except ImportError:  # pragma: no cover - dependency is declared for normal use.
        lines = [
            f'solace_lab_order_to_fill_latency_ns{{quantile="0.99"}} {report.p99_latency_ns}',
            f'solace_lab_order_to_fill_latency_ns{{quantile="0.999"}} {report.p999_latency_ns}',
            f"solace_lab_ingress_msg_sec {report.ingress_msg_sec}",
            f"solace_lab_egress_msg_sec {report.egress_msg_sec}",
            f"solace_lab_lost_messages_total {report.lost_messages}",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    registry = CollectorRegistry()
    latency = Gauge(
        "solace_lab_order_to_fill_latency_ns",
        "Order-to-fill latency in nanoseconds.",
        ["quantile", "run"],
        registry=registry,
    )
    throughput = Gauge(
        "solace_lab_messages_per_second",
        "Measured message rate.",
        ["direction", "run"],
        registry=registry,
    )
    loss = Gauge(
        "solace_lab_lost_messages_total",
        "Messages expected but not observed.",
        ["run"],
        registry=registry,
    )
    latency.labels("0.50", report.name).set(report.p50_latency_ns)
    latency.labels("0.99", report.name).set(report.p99_latency_ns)
    latency.labels("0.999", report.name).set(report.p999_latency_ns)
    throughput.labels("ingress", report.name).set(report.ingress_msg_sec)
    throughput.labels("egress", report.name).set(report.egress_msg_sec)
    loss.labels(report.name).set(report.lost_messages)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(generate_latest(registry))


def write_latency_plot(reports: Sequence[LatencyBenchmarkReport], path: Path) -> None:
    """Write a Plotly PNG showing tail latency across chaos levels."""

    if not reports:
        raise ValueError("at least one report is required")
    import plotly.graph_objects as go

    fig = go.Figure()
    load_values = sorted({report.target_orders_per_second for report in reports})
    for load in load_values:
        for persistent in (False, True):
            subset = sorted(
                (
                    report
                    for report in reports
                    if report.target_orders_per_second == load and report.persistent == persistent
                ),
                key=lambda report: report.chaos_level,
            )
            if not subset:
                continue
            suffix = "persistent" if persistent else "direct"
            fig.add_trace(
                go.Scatter(
                    x=[report.chaos_level for report in subset],
                    y=[report.p99_latency_ns / 1_000 for report in subset],
                    mode="lines+markers",
                    name=f"p99 {suffix} {load}/s",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=[report.chaos_level for report in subset],
                    y=[report.p999_latency_ns / 1_000 for report in subset],
                    mode="lines+markers",
                    name=f"p99.9 {suffix} {load}/s",
                    line={"dash": "dash"},
                )
            )
    fig.update_layout(
        title="Tail latency vs. chaos level and load",
        xaxis_title="chaos level",
        yaxis_title="latency (us)",
        template="plotly_white",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_image(path)


def parse_sdkperf_summary(text: str) -> dict[str, float]:
    """Extract stable numeric fields from SDKPerf summary output."""

    patterns = {
        "transmitted": r"Total Messages transmitted\s*=\s*([0-9,]+)",
        "received": r"Total Messages received across all subscribers\s*=\s*([0-9,]+)",
        "publish_rate": r"Computed publish rate \(msg/sec\)\s*=\s*([0-9,.]+)",
        "subscriber_rate": (
            r"Computed subscriber rate \(msg/sec across all subscribers\)\s*=\s*([0-9,.]+)"
        ),
        "p99_latency_us": r"99th percentile latency\s*=\s*([0-9,.]+)",
    }
    values: dict[str, float] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).replace(",", "")
            values[key] = float(raw)
    return values


def write_sdkperf_plot(csv_path: Path, output_path: Path) -> None:
    """Write a Plotly PNG comparing SDKPerf throughput to Solace targets."""

    import plotly.graph_objects as go

    with csv_path.open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{csv_path} has no SDKPerf rows")
    for row in rows:
        row["egress_msg_sec"] = float(row.get("egress_msg_sec") or 0)
        row["solace_target_msg_sec"] = float(row.get("solace_target_msg_sec") or 0)
    labels = [f"{row['case_name']} ({row['tls']})" for row in rows]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="local egress",
            x=labels,
            y=[float(row["egress_msg_sec"]) for row in rows],
        )
    )
    fig.add_trace(
        go.Bar(
            name="Solace software ESXi target",
            x=labels,
            y=[float(row["solace_target_msg_sec"]) for row in rows],
        )
    )
    fig.update_layout(
        barmode="group",
        title="SDKPerf throughput vs. Solace software ESXi targets",
        xaxis_title="case",
        yaxis_title="messages/sec",
        template="plotly_white",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_image(output_path)


def run_http_burst(
    venue_url: str,
    *,
    orders: int,
    symbol: str,
    output_path: Path,
) -> dict[str, object]:
    """Drive the venue HTTP endpoint with a deterministic crossing-order burst."""

    if orders < 2:
        raise ValueError("orders must be at least 2")
    latencies_ns: list[int] = []
    sent = 0
    accepted = 0
    start = time.perf_counter_ns()
    with httpx.Client(timeout=10.0) as client:
        for index in range(orders):
            side = "SELL" if index % 2 == 0 else "BUY"
            payload = {
                "order_id": f"http-perf-{index:012d}",
                "symbol": symbol,
                "side": side,
                "quantity": 1,
                "price": "100.00",
                "trader_id": f"http-{index % 10:02d}",
                "timestamp_ns": index + 1,
            }
            before = time.perf_counter_ns()
            response = client.post(f"{venue_url.rstrip('/')}/orders", json=payload)
            after = time.perf_counter_ns()
            sent += 1
            response.raise_for_status()
            accepted += 1
            latencies_ns.append(after - before)
    duration_ns = max(1, time.perf_counter_ns() - start)
    row = {
        "orders_sent": sent,
        "orders_accepted": accepted,
        "lost_messages": sent - accepted,
        "duration_ns": duration_ns,
        "ingress_msg_sec": sent / (duration_ns / NS_PER_SECOND),
        "p99_latency_ns": nearest_rank_percentile(latencies_ns, 99),
        "p999_latency_ns": nearest_rank_percentile(latencies_ns, 99.9),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    return row


def _run_cli_benchmark(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    chaos_levels = [float(value) for value in args.chaos_levels.split(",") if value]
    loads = [int(value) for value in args.load_rates.split(",") if value]
    reports: list[LatencyBenchmarkReport] = []
    for load in loads:
        for chaos_level in chaos_levels:
            for persistent in (False, True):
                reports.append(
                    run_order_to_fill_benchmark(
                        BenchmarkRunConfig(
                            total_orders=args.orders,
                            target_orders_per_second=load,
                            fanout=args.fanout,
                            persistent=persistent,
                            chaos_level=chaos_level,
                            symbol=args.symbol,
                        )
                    )
                )
    write_reports_csv(reports, output_dir / "chaos_latency.csv")
    for report in reports:
        write_prometheus_metrics(report, output_dir / f"{report.name}.prom")
    write_latency_plot(reports, output_dir / "chaos_tail_latency.png")


def _run_target(args: argparse.Namespace) -> None:
    target = solace_target(
        args.delivery_mode,
        args.pattern,
        args.message_size_bytes,
        fanout=args.fanout,
    )
    print(target.messages_per_second)


def _run_parse_sdkperf(args: argparse.Namespace) -> None:
    text = Path(args.input).read_text(encoding="utf-8", errors="replace")
    values = parse_sdkperf_summary(text)
    for key in sorted(values):
        print(f"{key}={values[key]}")


def _run_plot_sdkperf(args: argparse.Namespace) -> None:
    write_sdkperf_plot(Path(args.csv), Path(args.output))


def _run_http_burst_cli(args: argparse.Namespace) -> None:
    run_http_burst(
        args.venue_url,
        orders=args.orders,
        symbol=args.symbol,
        output_path=Path(args.output),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Solace Trading Lab performance utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run deterministic chaos latency benchmark.")
    run_parser.add_argument("--output-dir", required=True)
    run_parser.add_argument(
        "--orders",
        type=int,
        default=int(os.getenv("STL_CHAOS_ORDERS", "2000")),
    )
    run_parser.add_argument("--fanout", type=int, default=10)
    run_parser.add_argument("--symbol", default="FOO")
    run_parser.add_argument("--chaos-levels", default="0,0.25,0.5,1.0")
    run_parser.add_argument("--load-rates", default="100000,1000000")
    run_parser.set_defaults(func=_run_cli_benchmark)

    target_parser = subparsers.add_parser("target", help="Print one Solace ESXi target.")
    target_parser.add_argument("--delivery-mode", choices=["direct", "persistent"], required=True)
    target_parser.add_argument("--pattern", choices=["p2p", "fanout"], required=True)
    target_parser.add_argument("--message-size-bytes", type=int, required=True)
    target_parser.add_argument("--fanout", type=int, default=1)
    target_parser.set_defaults(func=_run_target)

    parse_parser = subparsers.add_parser("parse-sdkperf", help="Parse SDKPerf summary output.")
    parse_parser.add_argument("--input", required=True)
    parse_parser.set_defaults(func=_run_parse_sdkperf)

    plot_parser = subparsers.add_parser("plot-sdkperf", help="Plot SDKPerf CSV results.")
    plot_parser.add_argument("--csv", required=True)
    plot_parser.add_argument("--output", required=True)
    plot_parser.set_defaults(func=_run_plot_sdkperf)

    http_parser = subparsers.add_parser("http-burst", help="Drive venue HTTP burst benchmark.")
    http_parser.add_argument("--venue-url", default="http://localhost:9001")
    http_parser.add_argument("--orders", type=int, default=1000)
    http_parser.add_argument("--symbol", default="FOO")
    http_parser.add_argument("--output", required=True)
    http_parser.set_defaults(func=_run_http_burst_cli)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
