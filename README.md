# Solace Trading Lab

A deterministic, event-driven trading systems lab for learning limit order
books, market data, strategy behavior, replay, and infrastructure effects with
Solace PubSub+ as the message backbone.

This is not a real trading system. It is an educational simulator.

## What Is Included

- Price-time priority limit order book with queue position tracking
- Matching engine that publishes order acks, fills, and book snapshots
- Solace PubSub+ wrapper plus deterministic in-memory bus for tests
- Market data feed that derives L1, L2, and trade-print topics
- Market maker strategy with inventory-aware quote skew
- Latency chaos injector and replay recorder/player
- Docker Compose for a local Solace PubSub+ broker

## Requirements

- Python 3.12+
- `uv`
- Docker and Docker Compose

## Quick Start

Install dependencies and run tests:

```bash
uv sync
uv run pytest
```

Start the Solace broker:

```bash
./scripts/setup_solace.sh
```

Run the exchange, market data feed, and example strategy:

```bash
./scripts/run_all.sh
```

The broker management UI is available at `http://localhost:8080` with
`admin/admin`. The exchange HTTP helper runs at `http://localhost:9001`.

## Submit A Test Order

With the services running, submit a resting sell order:

```bash
curl -X POST http://localhost:9001/orders \
  -H 'content-type: application/json' \
  -d '{
    "order_id": "demo-sell-1",
    "symbol": "FOO",
    "side": "SELL",
    "quantity": 10,
    "price": "100.00",
    "trader_id": "demo"
  }'
```

Then submit a crossing buy order:

```bash
curl -X POST http://localhost:9001/orders \
  -H 'content-type: application/json' \
  -d '{
    "order_id": "demo-buy-1",
    "symbol": "FOO",
    "side": "BUY",
    "quantity": 4,
    "price": "100.00",
    "trader_id": "demo"
  }'
```

Inspect the book:

```bash
curl http://localhost:9001/book/FOO
```

## Topic Layout

- `solace-lab/exchange/orders/{symbol}`
- `solace-lab/exchange/acks/{symbol}`
- `solace-lab/exchange/fills/{symbol}`
- `solace-lab/exchange/book/{symbol}`
- `solace-lab/md/L1/{symbol}`
- `solace-lab/md/L2/{symbol}`
- `solace-lab/md/trades/{symbol}`
- `solace-lab/chaos/events`
- `solace-lab/replay/control`

## Determinism

The core exchange is pure and single-threaded. Tests inject deterministic clocks
and use explicit order ids. Replay stores ordered JSONL messages so the same
input sequence can be published again without wall-clock timing unless realtime
playback is requested.

## Development

Run a single test file:

```bash
uv run pytest tests/test_orderbook.py
```

Run services against the in-memory bus for local single-process experiments by
setting:

```bash
export STL_IN_MEMORY_BUS=true
```
