# Design note: event ordering and replay

The central question is how to make an event-driven pipeline inspectable: what order did commands arrive in, which state transition produced a fill, and can the same input produce the same output in a fresh process?

## Matching and projections

`OrderBook` stores FIFO queues per price level, uses Decimal prices and integer quantities, and tracks seen order IDs. `MatchingEngine` publishes acknowledgements, fills and book snapshots. `MarketDataFeed` projects those messages into L1, L2 and trade channels. Queue position is measured in remaining units ahead at the same price.

The price levels use dictionaries with min/max scans and snapshots sort prices. This makes the implementation readable, but best-price lookup and snapshot construction are not optimized for large books. It is a correctness-oriented Python model.

## Time and reproducibility

The default clock is wall time. Tests and the replay demo inject a deterministic clock. Replaying fixed commands into a fresh engine produces comparable venue and market-data payloads. Transport receipt timestamps remain wall-clock observations and are excluded from the demo digest.

The recorder appends JSONL with a recording sequence. The player sorts on that sequence; it does not currently reject gaps or duplicates. A recorder restart resets its counter even when appending to an existing file. Production event sourcing would require sequence continuity checks, explicit stream identity and restart semantics before relying on this format.

## Transport semantics

The in-memory bus dispatches synchronously and queues reentrant publications. The Solace adapter uses direct publishing and direct subscriptions. Neither this interface nor the offline tests establish durable order flow, exactly-once delivery or atomic state-and-event persistence. Real broker reconnect behavior needs a separate integration test.

Replay is intended for a fresh isolated instance. Replaying recorded outputs together with input commands into active services can create duplicate downstream events. A production design should distinguish commands, derived events and side effects explicitly.

## Evidence and next experiments

Run `uv run --frozen --no-editable pytest` and `uv run --frozen --no-editable python examples/replay_demo.py`. The demo checks expected fills, rejection and final depth in addition to comparing digests across runs. A stable digest alone cannot establish correctness.

The clock, network and gateway modules are exploratory timing models. There is no measured P&L result or hardware benchmark in this repository. Useful follow-up experiments would declare a workload, baseline, measurement clock, machine, warm-up and repetitions, then publish raw observations and uncertainty alongside the producing commit.

Development used AI assistance. Repository tests and an explanation of these tradeoffs support a technical discussion; this project alone is not evidence of unaided coding or real exchange operations.
