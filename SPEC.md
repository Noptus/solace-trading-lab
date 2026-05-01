# 🚀 Solace Trading Lab — Codex Build Specification

## Overview

Solace Trading Lab is a deterministic, event-driven trading systems laboratory.

It simulates a realistic limit order book, market participants, and infrastructure constraints (latency, replay, failures) using Solace PubSub+ as the event backbone.

👉 This is NOT a real trading system.  
👉 This is a learning + experimentation + benchmarking platform.

---

## 🎯 Goals

Build a platform that allows users to:

- Understand market microstructure (order books, fills, queue position)
- Learn event-driven architecture using Solace PubSub+
- Measure how infrastructure (latency, backpressure, failures) impacts trading outcomes
- Replay scenarios deterministically
- Benchmark distributed systems in trading contexts

Solace PubSub+ acts as the event broker connecting services in a publish/subscribe architecture.

---

## 🧠 Target Users

1. Engineers preparing for trading infrastructure roles (Optiver, IMC, etc.)
2. Developers learning event-driven systems with Solace
3. Students learning market microstructure
4. Teams demonstrating event-driven architecture

---

## 🏗️ Architecture

### Services (Python microservices communicating via Solace PubSub+)

```
┌────────────────────────────────────────────────────┐
│              Solace PubSub+ Broker                  │
│         (Docker: solace/solace-pubsub-standard)     │
└──────────┬──────────┬──────────┬──────────┬────────┘
           │          │          │          │
  ┌────────▼───┐ ┌────▼─────┐ ┌─▼────────┐ ┌▼──────────┐
  │  Exchange  │ │  Market  │ │ Strategy │ │  Infra    │
  │  Engine    │ │  Data    │ │  Engine  │ │  Chaos    │
  │            │ │  Feed    │ │          │ │  Agent    │
  └────────────┘ └──────────┘ └──────────┘ └───────────┘
```

### 1. Exchange Engine (`exchange/`)
- Maintains a **price-time priority limit order book**
- Processes: NewOrder, Cancel, Amend
- Publishes: OrderAck, Fill, Reject
- Deterministic matching: same input → same output
- Tracks queue position per order

### 2. Market Data Feed (`market_data/`)
- Subscribes to Exchange fills and book updates
- Publishes: L1 (BBO), L2 (depth), Trade prints
- Supports configurable latency injection (simulating network delay)
- Publishes on topics like `md/L1/{symbol}`, `md/L2/{symbol}`, `md/trades/{symbol}`

### 3. Strategy Engine (`strategy/`)
- Subscribes to Market Data
- Implements simple strategies:
  - **MarketMaker**: quotes around mid, manages inventory
  - **MomentumTaker**: detects short-term momentum, sends aggressive orders
  - **MeanReversion**: fades large moves
- Publishes orders to Exchange via Solace
- Tracks P&L, fills, latency metrics

### 4. Infrastructure Chaos Agent (`chaos/`)
- Injects realistic infrastructure failures:
  - Latency spikes (add N ms delay to message delivery)
  - Message drops (simulate packet loss)
  - Broker disconnection / reconnection
  - Backpressure (slow consumer scenarios)
- Configurable via YAML scenarios

### 5. Replay Engine (`replay/`)
- Records all Solace messages with timestamps
- Replays scenarios deterministically
- Allows "what-if" analysis (replay with different strategy params)

### 6. Dashboard (`dashboard/`)
- Simple web UI (FastAPI + HTMX or Streamlit)
- Real-time order book visualization
- P&L curves
- Latency histograms
- Infrastructure event timeline

---

## 📡 Solace Integration

### Connection
- Use `solace-pubsubplus` Python package (PyPI)
- Connect via SMF protocol to local Docker broker
- Use Direct Messaging for market data (fire-and-forget, low latency)
- Use Guaranteed Messaging for order flow (persistent, exactly-once)

### Topic Hierarchy
```
solace-lab/exchange/orders/{symbol}          # Order submissions
solace-lab/exchange/acks/{symbol}            # Order acknowledgments
solace-lab/exchange/fills/{symbol}           # Fill notifications
solace-lab/md/L1/{symbol}                   # Best bid/offer
solace-lab/md/L2/{symbol}                   # Full depth
solace-lab/md/trades/{symbol}               # Trade prints
solace-lab/chaos/events                     # Infrastructure events
solace-lab/replay/control                   # Replay commands
```

### Docker Compose
```yaml
services:
  solace:
    image: solace/solace-pubsub-standard:latest
    ports:
      - "8080:8080"   # SEMP management
      - "55555:55555" # SMF
      - "8008:8008"   # Web transport
    environment:
      - username_admin_globalaccesslevel=admin
      - username_admin_password=admin
    shm_size: '1g'
```

---

## 📁 Project Structure

```
solace-trading-lab/
├── docker-compose.yml
├── pyproject.toml              # uv/poetry project config
├── README.md
├── SPEC.md
├── src/
│   ├── common/
│   │   ├── __init__.py
│   │   ├── models.py           # Pydantic models (Order, Fill, etc.)
│   │   ├── solace_client.py    # Solace connection wrapper
│   │   └── config.py           # Settings via pydantic-settings
│   ├── exchange/
│   │   ├── __init__.py
│   │   ├── orderbook.py        # Limit order book implementation
│   │   ├── matching_engine.py  # Price-time priority matching
│   │   └── main.py             # Exchange service entry point
│   ├── market_data/
│   │   ├── __init__.py
│   │   ├── feed.py             # Market data publisher
│   │   └── main.py
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── base.py             # Base strategy class
│   │   ├── market_maker.py
│   │   ├── momentum.py
│   │   ├── mean_reversion.py
│   │   └── main.py
│   ├── chaos/
│   │   ├── __init__.py
│   │   ├── agent.py            # Chaos injection logic
│   │   ├── scenarios.py        # YAML scenario loader
│   │   └── main.py
│   ├── replay/
│   │   ├── __init__.py
│   │   ├── recorder.py
│   │   ├── player.py
│   │   └── main.py
│   └── dashboard/
│       ├── __init__.py
│       ├── app.py              # FastAPI app
│       ├── templates/
│       └── main.py
├── scenarios/
│   ├── normal_trading.yaml
│   ├── latency_spike.yaml
│   └── broker_disconnect.yaml
├── tests/
│   ├── test_orderbook.py
│   ├── test_matching.py
│   ├── test_strategies.py
│   └── test_replay.py
└── scripts/
    ├── run_all.sh
    └── setup_solace.sh
```

---

## 🔧 Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12+ |
| Broker | Solace PubSub+ (Docker) |
| Messaging | solace-pubsubplus (PyPI) |
| Models | Pydantic v2 |
| Config | pydantic-settings |
| Web | FastAPI + HTMX |
| Testing | pytest |
| Package mgr | uv |
| Containers | Docker Compose |

---

## 🧪 Implementation Priorities

### Phase 1: Core (MVP)
1. Order book + matching engine (fully tested)
2. Solace client wrapper (connect, pub, sub)
3. Exchange service (process orders, publish fills)
4. Market data feed (L1/L2 from exchange events)
5. One strategy (MarketMaker) subscribing + trading
6. Docker Compose with Solace broker

### Phase 2: Scenarios
7. Chaos agent with latency injection
8. Replay engine (record + playback)
9. Additional strategies (Momentum, MeanReversion)
10. Scenario YAML configs

### Phase 3: Polish
11. Dashboard with real-time book visualization
12. P&L tracking and reporting
13. Documentation and tutorials
14. Benchmarking harness

---

## 📐 Key Design Decisions

1. **Deterministic**: Given same inputs + same random seed, produce identical outputs
2. **Event-sourced**: All state changes flow through Solace messages
3. **Decoupled**: Each service only knows about message schemas, not other services
4. **Realistic**: Order book uses price-time priority, tracks queue position
5. **Observable**: Every message is timestamped and can be replayed
6. **Educational**: Code is clean, well-commented, with docstrings explaining WHY

---

## 🚦 Success Criteria

- [ ] Can run `docker-compose up` and have full system operational
- [ ] Exchange correctly matches orders with price-time priority
- [ ] Market maker strategy quotes and gets filled
- [ ] Injecting 100ms latency visibly degrades strategy P&L
- [ ] Replay produces identical results
- [ ] All core components have >80% test coverage
- [ ] README explains setup in under 5 minutes
