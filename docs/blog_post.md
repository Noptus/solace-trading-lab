# Building a Deterministic Trading Systems Lab

Most trading infrastructure interviews test the same things: order book mechanics, event-driven architecture, latency sensitivity, and the ability to reason about distributed system failures. Instead of studying these concepts in isolation, I built a lab that ties them together.

## The Problem

Trading systems are notoriously hard to reason about because state is distributed across multiple processes communicating via messages. A fill that arrives 2ms late can flip a strategy from profitable to underwater. But you cannot experiment with this unless you have a deterministic replay mechanism and a realistic order book.

Existing open-source matching engines either skip the infrastructure layer entirely (pure data structures, no messaging) or are too complex to learn from (full FIX protocol stacks with 50K LOC). I wanted something in between: a complete event loop from order submission to fill notification, running over a real message broker, with the ability to inject failures and replay scenarios identically.

## Architecture

```
Producer (Strategy) --> Solace PubSub+ --> Exchange Engine --> Solace PubSub+ --> Consumer (Strategy)
                              |                                       |
                        Chaos Agent                            Market Data Feed
                     (latency injection)                    (L1/L2/trade prints)
                              |
                        Replay Engine
                     (record + playback)
```

Every component communicates exclusively through Solace topics. The exchange never calls the strategy directly. The market data feed never queries the exchange. This forces the same decoupling you see in production systems at firms like Optiver or IMC.

## Order Book Implementation

The book uses price-time priority with explicit queue position tracking. Each resting order knows its position in the queue at its price level. When a new aggressive order arrives, it walks the book from best price inward, filling resting orders in FIFO sequence.

Key design choices:

- **Decimal arithmetic everywhere.** Floating point in a matching engine is a compliance violation waiting to happen. Every price and quantity is `decimal.Decimal`.
- **Deterministic timestamps.** The engine uses a monotonic nanosecond counter, not wall clock time. Same input sequence always produces same output sequence.
- **Queue position is a first-class concept.** Strategies can query "where am I in the queue at price X?" because this determines fill probability. Most educational implementations ignore this entirely.

## Solace as the Event Backbone

I chose Solace PubSub+ over Kafka or RabbitMQ for several reasons:

1. **Topic hierarchy with wildcards.** Publishing to `lab/exchange/fills/AAPL` and subscribing to `lab/exchange/fills/>` gives you flexible routing without consumer groups or partition assignment.
2. **Direct messaging mode.** For market data, you want fire-and-forget semantics with no persistence overhead. Solace supports both guaranteed (for order flow) and direct (for market data) on the same broker.
3. **Single Docker container.** No Zookeeper, no cluster setup. `docker compose up` and you have a broker.

The client wrapper (`src/common/solace_client.py`) abstracts connection lifecycle and provides a clean publish/subscribe interface. For tests, an `InMemoryBroker` swaps in with identical semantics but zero network overhead.

## Infrastructure Chaos

The chaos agent injects three categories of failures:

- **Latency spikes.** Delays message delivery by N milliseconds. Watch your market maker's P&L curve bend when data arrives 50ms late.
- **Message drops.** Simulates packet loss. Does your strategy handle a missing fill ack gracefully, or does it double-send?
- **Broker disconnection.** Kills the Solace connection for K seconds. Does your strategy detect the gap and re-sync state?

Scenarios are defined in YAML. You can replay the same market conditions with and without failures to measure the exact P&L impact of infrastructure quality.

## Replay

Every message that flows through the broker gets recorded with its nanosecond timestamp and topic. The replay engine re-publishes these messages in the original sequence, driving the exchange and strategies identically. This enables:

- **Regression testing.** Change a strategy parameter, replay yesterday, compare P&L.
- **Failure analysis.** "What would have happened if that latency spike hadn't occurred?"
- **Onboarding.** New engineers can step through a recorded session tick by tick.

## Running It

```bash
git clone https://github.com/Noptus/solace-trading-lab.git
cd solace-trading-lab
uv sync
docker compose up -d
uv run pytest  # verify everything works
./scripts/run_all.sh  # start exchange + market data + strategy
```

The market maker strategy starts quoting immediately. Submit orders via the exchange HTTP endpoint and watch fills flow through the system.

## What I Learned

Building this reinforced a few things I already suspected but had never verified empirically:

1. **Queue position matters more than signal quality for passive strategies.** A strategy that is first in queue at a slightly worse price consistently outperforms one that is last in queue at the theoretical best price.
2. **50ms of added latency costs roughly 15-20% of market making P&L** in the simulated environment. Real production systems see similar degradation.
3. **Message ordering guarantees from the broker are not the same as timestamp ordering.** Your strategy must handle out-of-order arrivals even with "guaranteed" delivery.

The full source is at [github.com/Noptus/solace-trading-lab](https://github.com/Noptus/solace-trading-lab). It is meant to be read, modified, and broken. Pull requests welcome.
