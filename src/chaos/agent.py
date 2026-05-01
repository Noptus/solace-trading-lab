"""Deterministic broker fault injection."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from common.config import Settings, Topics, get_settings
from common.models import ChaosEvent, ChaosEventKind
from common.solace_client import MessageBus, SolaceClient


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    """Latency distribution for a deterministic experiment."""

    fixed_ms: int = 0
    jitter_ms: int = 0
    probability: float = 1.0
    drop_probability: float = 0.0
    seed: int = 1

    def __post_init__(self) -> None:
        if self.fixed_ms < 0 or self.jitter_ms < 0:
            raise ValueError("latency values must be non-negative")
        if not 0 <= self.probability <= 1:
            raise ValueError("probability must be in [0, 1]")
        if not 0 <= self.drop_probability <= 1:
            raise ValueError("drop_probability must be in [0, 1]")


class LatencyInjector:
    """Apply deterministic latency and drops according to a profile."""

    def __init__(self, profile: LatencyProfile) -> None:
        self.profile = profile
        self._rng = random.Random(profile.seed)

    def should_drop(self) -> bool:
        """Return whether this message should be dropped."""

        if self.profile.drop_probability == 0:
            return False
        return self._rng.random() < self.profile.drop_probability

    def delay_ms(self) -> int:
        """Return the next deterministic delay."""

        if self.profile.probability == 0:
            return 0
        if self.profile.probability < 1 and self._rng.random() > self.profile.probability:
            return 0
        jitter = self._rng.randint(0, self.profile.jitter_ms) if self.profile.jitter_ms else 0
        return self.profile.fixed_ms + jitter

    def apply(self) -> tuple[int, bool]:
        """Sleep for the next delay and return (delay_ms, dropped)."""

        dropped = self.should_drop()
        if dropped:
            return 0, True
        delay = self.delay_ms()
        if delay > 0:
            time.sleep(delay / 1000)
        return delay, False


class ChaosAgent:
    """Forward messages and emit latency/drop audit events."""

    def __init__(
        self,
        bus: MessageBus | None = None,
        *,
        settings: Settings | None = None,
        latency: LatencyInjector | None = None,
    ) -> None:
        self.settings = (settings or get_settings()).normalized()
        self.topics = Topics(self.settings.topic_prefix)
        self.bus = bus or SolaceClient(self.settings)
        self.latency = latency or LatencyInjector(LatencyProfile())
        self._sequence = 0

    def forward(self, topic: str, payload: dict[str, object]) -> ChaosEvent:
        """Publish with deterministic delay/drop accounting."""

        self._sequence += 1
        delay, dropped = self.latency.apply()
        kind = ChaosEventKind.DROP if dropped else ChaosEventKind.LATENCY
        event = ChaosEvent(
            kind=kind,
            topic=topic,
            delay_ms=delay,
            dropped=dropped,
            sequence=self._sequence,
        )
        self.bus.publish(self.topics.chaos_events(), event)
        if not dropped:
            self.bus.publish(topic, payload)
        return event

    def latency_hook(self, topic: str, _payload: dict[str, object]) -> None:
        """Hook compatible with MarketDataFeed latency injection."""

        self._sequence += 1
        delay, dropped = self.latency.apply()
        kind = ChaosEventKind.DROP if dropped else ChaosEventKind.LATENCY
        self.bus.publish(
            self.topics.chaos_events(),
            ChaosEvent(
                kind=kind,
                topic=topic,
                delay_ms=delay,
                dropped=dropped,
                sequence=self._sequence,
            ),
        )
