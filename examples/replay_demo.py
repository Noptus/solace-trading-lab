"""Replay fixed order commands through venue and market-data projections."""

from __future__ import annotations

import hashlib
import itertools
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from common.config import Settings, Topics
from common.models import RecordedMessage
from common.solace_client import InMemoryBroker
from exchange.matching_engine import MatchingEngine
from market_data.feed import MarketDataFeed
from replay.player import ReplayPlayer


def commands() -> list[dict]:
    def order(order_id: str, side: str, quantity: int, trader: str) -> dict:
        return dict(order_id=order_id, symbol="DEMO", side=side, quantity=quantity,
                    price="100.00", trader_id=trader, timestamp_ns=1)

    return [
        order("ask-1", "SELL", 5, "seller-1"),
        order("ask-2", "SELL", 7, "seller-2"),
        order("buy-1", "BUY", 8, "buyer"),
        order("buy-1", "BUY", 8, "buyer"),  # duplicate must not trade again
        dict(command="CANCEL", order_id="ask-2", symbol="DEMO",
             trader_id="seller-2", timestamp_ns=2),
    ]


def run_once(path: Path) -> dict:
    settings = Settings(topic_prefix="demo", in_memory_bus=True)
    topics = Topics(settings.topic_prefix)
    bus = InMemoryBroker()
    ticks = itertools.count(1_000)
    engine = MatchingEngine(bus, settings=settings, clock=lambda: next(ticks))
    engine.start()
    MarketDataFeed(bus, settings=settings).start()
    ReplayPlayer(path, bus, settings=settings).play()
    events = [dict(topic=e.topic, payload=e.payload) for e in bus.published]
    fills = [e.payload for e in bus.published if e.topic == topics.venue_fills("DEMO")]
    rejected = [e.payload for e in bus.published
                if e.topic == topics.venue_acks("DEMO") and e.payload["status"] == "REJECTED"]
    # These semantic checks provide an oracle independent of digest equality.
    if [(f["maker_order_id"], f["quantity"], Decimal(f["price"])) for f in fills] != [
        ("ask-1", 5, Decimal("100.00")), ("ask-2", 3, Decimal("100.00"))
    ]:
        raise ValueError("unexpected FIFO fills")
    if len(rejected) != 1 or rejected[0]["reason"] != "duplicate order id":
        raise ValueError("duplicate order was not rejected")
    snapshot = engine.snapshot("DEMO")
    if snapshot.bids or snapshot.asks:
        raise ValueError("cancel did not leave an empty book")
    if not any(e.topic == topics.md_trades("DEMO") for e in bus.published):
        raise ValueError("market-data projection did not run")
    digest = hashlib.sha256(json.dumps(events, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    bus.close()
    return dict(events=len(events), fills=len(fills), filled_quantity=sum(f["quantity"] for f in fills),
                duplicate_rejections=len(rejected), final_book="empty", output_sha256=digest)


def main() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "commands.jsonl"
        topic = Topics("demo").venue_orders("DEMO")
        records = [RecordedMessage(sequence=i, topic=topic, payload=payload, timestamp_ns=i)
                   for i, payload in enumerate(commands(), start=1)]
        path.write_text("".join(record.to_json() + "\n" for record in records))
        first, second = run_once(path), run_once(path)
        if first != second:
            raise ValueError("replay produced different outputs in fresh engines")
        print(json.dumps(dict(status="PASS", repeatable=True, **first), indent=2))


if __name__ == "__main__":
    main()
