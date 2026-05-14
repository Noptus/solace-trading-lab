# Solace Trading Lab

A deterministic, event-driven trading infrastructure laboratory for studying market microstructure, order matching, and the impact of infrastructure on trading performance. Built around Solace PubSub+ for realistic message-broker semantics.

**v0.2** adds Jump Trading-grade infrastructure simulation: White Rabbit precision timing, FPGA gateways, kernel bypass, SBE wire protocols, smart order routing, and full tick-to-trade measurement.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SOLACE PUBSUB+ BROKER                        │
│                    (or deterministic in-memory bus)                  │
└────────┬──────────┬──────────┬──────────┬──────────┬───────────────┘
         │          │          │          │          │
    ┌────▼───┐ ┌───▼────┐ ┌──▼───┐ ┌───▼───┐ ┌───▼────┐
    │Exchange│ │Market  │ │Strat │ │ Chaos │ │Replay  │
    │Venue   │ │Data    │ │Engine│ │ Agent │ │Recorder│
    └────┬───┘ └───┬────┘ └──┬───┘ └───┬───┘ └───┬────┘
         │         │         │         │          │
    ┌────▼─────────▼─────────▼─────────▼──────────▼────┐
    │              NEW IN v0.2 — INFRA LAYER            │
    ├───────────────────────────────────────────────────┤
    │  🕐 White Rabbit Clock   │  🔌 FPGA Gateway       │
    │  • Sub-ns PTP sync       │  • 5-stage pipeline    │
    │  • Clock domains         │  • 50-200ns logic      │
    │  • Drift/jitter model    │  • HW risk gate        │
    ├──────────────────────────┼────────────────────────┤
    │  🌐 Network Topology     │  ⚡ Wire Protocol       │
    │  • Co-lo tiers           │  • SBE binary codec    │
    │  • Smart order routing   │  • Hot/cold paths      │
    │  • Cross-connects        │  • Lock-free SPSC      │
    │  • Multi-venue latency   │  • Tick-to-trade <1μs  │
    └──────────────────────────┴────────────────────────┘
```

## Modules

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `exchange` | Price-time priority matching engine | `OrderBook`, `MatchingEngine` |
| `market_data` | L1/L2/trade projection from venue events | `MarketDataFeed` |
| `strategy` | Base strategy framework + market maker | `Strategy`, `MarketMaker` |
| `chaos` | Deterministic fault injection | `ChaosAgent`, `LatencyInjector` |
| `replay` | Record and replay message streams | `ReplayRecorder`, `ReplayPlayer` |
| `clock` | White Rabbit PTP clock simulation | `WhiteRabbitClock`, `ClockDomain`, `TimestampAuthority` |
| `gateway` | FPGA gateway and kernel bypass | `FPGAGateway`, `KernelBypass`, `RiskGate` |
| `network` | Topology, co-location, routing | `ExchangeTopology`, `SmartOrderRouter`, `CrossConnect` |
| `wire` | Binary protocol and hot/cold separation | `SBEMessage`, `HotPath`, `ColdPath`, `TickToTrade` |

## Quick Start

```bash
# Clone and install
git clone https://github.com/Noptus/solace-trading-lab.git
cd solace-trading-lab
uv sync  # or: pip install -e .

# Run tests (no Solace broker needed — uses in-memory bus)
pytest

# Start Solace PubSub+ (optional, for real broker testing)
docker compose up -d
./scripts/setup_solace.sh

# Run all services
./scripts/run_all.sh
```

## White Rabbit Precision Timing

Models the [White Rabbit](https://white-rabbit.web.cern.ch/) sub-nanosecond synchronization protocol used in trading infrastructure:

```python
from clock.white_rabbit import WhiteRabbitClock, ClockDomain, TimestampAuthority

# Create a clock with realistic drift and jitter
clock = WhiteRabbitClock(drift_ppb=0.1, jitter_ps=50, seed=42)
t1 = clock.now_ns()

# Model a clock domain (grandmaster + boundary clocks)
domain = ClockDomain(grandmaster_id="gm-01")
domain.add_node("switch-a", fiber_length_m=15.0, drift_ppb=0.05)
domain.add_node("nic-b", fiber_length_m=42.0, drift_ppb=0.2)

# Measure propagation delay (fiber: ~4.9ns/m)
delay = domain.propagation_delay_ns("gm-01", "nic-b")  # ~206ns

# Cross-clock comparison with uncertainty bounds
authority = TimestampAuthority(domain)
authority.calibrate("switch-a")
ts_a = authority.stamp("order-received", "switch-a")
ts_b = authority.stamp("order-acked", "nic-b")
comparison = authority.compare(ts_a, ts_b)
print(f"Delta: {comparison.delta_ns}ns ± {comparison.uncertainty_ns}ns")
```

## FPGA Gateway

Simulates hardware-accelerated order entry with sub-microsecond latency:

```python
from gateway.fpga import FPGAGateway, RiskGate, create_gateway

# Create an aggressive-profile gateway (Jump Trading style)
gw = create_gateway("aggressive")

# Submit order through 5-stage FPGA pipeline
result = gw.submit_order(order)
print(f"Wire latency: {result.egress_ts.corrected_ns - result.ingress_ts.corrected_ns}ns")

# Fast cancel path (~30ns)
gw.cancel_order("order-123")

# Hardware risk gate
risk = RiskGate(max_position=1000, max_notional=1_000_000, max_orders_per_second=50_000)
passed, reason = risk.check(order)
```

## Network Topology and Smart Order Routing

Model multi-venue infrastructure with realistic co-location latencies:

```python
from network.topology import ExchangeTopology, SmartOrderRouter, CoLocationTier

# Build default topology: London <-> Frankfurt <-> Chicago <-> NYC
topo = ExchangeTopology.build_default()

# Check inter-venue latency
matrix = topo.latency_matrix()
lse_to_cme = matrix["LSE"]["CME"]  # ~32ms (transatlantic fiber)

# Smart order routing with latency-adjusted fair value
router = SmartOrderRouter(topo, home_venue="LSE")
venue, latency = router.route(order, venue_books)

# IOC spray across venues
allocations = router.spray(order, ["LSE", "Eurex", "CME"], [0.5, 0.3, 0.2])
```

## Wire Protocol (SBE) and Hot/Cold Path

Binary encoding and lock-free architecture for tick-to-trade under 1 microsecond:

```python
from wire.protocol import SBEMessage, HotPath, ColdPath, TickToTrade

# SBE encode/decode (fixed size, zero-copy friendly)
sbe = SBEMessage()
raw = sbe.encode(order)  # Fixed 128 bytes, deterministic
decoded = sbe.decode(raw)

# Hot path: pre-allocated, lock-free, zero-allocation
hot = HotPath(buffer_count=4096, buffer_size=256)
result = hot.process(raw)

# Tick-to-trade measurement
t2t = TickToTrade()
t2t.start_measurement("event-001")
t2t.checkpoint("event-001", Stage.MARKET_DATA_RECV)
t2t.checkpoint("event-001", Stage.SIGNAL_COMPUTE)
t2t.checkpoint("event-001", Stage.NIC_EGRESS)
result = t2t.complete("event-001")
print(f"Total: {result.total_ns}ns, meets target: {result.meets_target}")
```

## Scenarios

YAML-driven experiments in `scenarios/`:

| Scenario | Description |
|----------|-------------|
| `normal_trading.yaml` | Zero-latency baseline |
| `latency_spike.yaml` | 100ms + 25ms jitter injection |
| `broker_disconnect.yaml` | Reconnection behavior (Phase 2) |

## Design Principles

1. **Deterministic by default** — Seeded RNGs, reproducible latency models, in-memory broker for tests
2. **Infrastructure is the alpha** — Model everything that affects tick-to-trade: clocks, NICs, fiber, FPGA pipelines
3. **Jump Trading grade** — Sub-microsecond order paths, hardware timestamping, kernel bypass, SBE encoding
4. **Composable** — Each module works standalone or wired together via Solace topics
5. **Observable** — Every event carries nanosecond timestamps, sequence numbers, and provenance

## Testing

```bash
pytest                          # All tests (241+)
pytest tests/test_white_rabbit.py   # Clock module
pytest tests/test_gateway.py        # FPGA gateway
pytest tests/test_network.py        # Topology and routing
pytest tests/test_wire_protocol.py  # Wire protocol
pytest tests/test_matching.py       # Exchange matching
pytest tests/test_orderbook.py      # Order book
pytest tests/test_strategies.py     # Strategy framework
```

## Extending

- Add new strategies by subclassing `strategy.base.Strategy`
- Add venues to the topology with `ExchangeTopology.add_venue()`
- Create custom clock domains for multi-datacenter experiments
- Plug FPGA gateway into the matching engine for end-to-end latency simulation
- Write new chaos scenarios in YAML

## License

MIT
