# Solace Trading Lab

Deterministic trading infrastructure lab for order matching, market data, replay, and faults.
Runs a simulated venue over Solace PubSub+ with HTTP endpoints for local control.

## Run

```bash
uv sync
uv run pytest
```

```bash
./scripts/setup_solace.sh
./scripts/run_all.sh
```

Submit a resting order:

```bash
curl -X POST http://localhost:9001/orders \
  -H 'content-type: application/json' \
  -d '{"order_id":"sell-1","symbol":"FOO","side":"SELL","quantity":10,"price":"100.00"}'
```

Inspect venue state:

```bash
curl http://localhost:9001/book/FOO
```

## Architecture

```text
                +------------------+
                | Solace PubSub+   |
                +------------------+
                  ^      ^      ^
                  |      |      |
+----------+   orders   fills   book     +-------------+
| Strategy | ---------> Venue ----------> | Market Data |
+----------+             |                +-------------+
                         |
                      recorder
                         |
                      replay
```

Core modules:

- `src/exchange/orderbook.py`: deterministic price-time priority book
- `src/exchange/matching_engine.py`: command routing and event publication
- `src/market_data/feed.py`: L1, L2, and trade-print projection
- `src/strategy/market_maker.py`: deterministic example strategy
- `src/replay`: JSONL recording and playback
- `src/chaos`: deterministic latency injection

## Extend

Add schemas in `src/common/models.py`, route topics through `src/common/config.py`, and keep
matching logic isolated from transport code. Use `InMemoryBroker` in tests and inject clocks for
deterministic assertions. Add scenario files under `scenarios/` for repeatable fault cases.
