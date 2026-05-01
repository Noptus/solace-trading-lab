"""Deterministic infrastructure fault injection.

The chaos agent is intentionally a small composable layer instead of a hidden
global monkeypatch.  Services can opt into latency injection by calling an
injector before publishing or by using `ChaosAgent.forward` in demos.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from common.config import Settings, Topics, get_settings
from common.models import ChaosEvent, ChaosEventKind
from common.solace_client import MessageBus, SolaceClient


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    """Latency distribution used for one deterministic experiment."""

    fixed_ms: int = 0
    jitter_ms: int = 0
    probability: float = 1.0
    seed: int = 1

    def __post_init__(self) -> None:
        if self.fixed_ms < 0 or self.jitter_ms < 0:
            raise ValueError("latency values must be non-negative")
        if not 0 <= self.probability <= 1:
            raise ValueError("probability must be in [0, 1]")


class LatencyInjector:
    """Apply deterministic latency according to a profile."""

    def __init__(self, profile: LatencyProfile) -> None:
        self.profile = profile
        self._rng = random.Random(profile.seed)

    def delay_ms(self) -> int:
        """Return the next deterministic delay without sleeping."""

        if self.profile.probability == 0:
            return 0
        if self.profile.probability < 1 and self._rng.random() > self.profile.probability:
            return 0
        jitter = self._rng.randint(0, self.profile.jitter_ms) if self.profile.jitter_ms else 0
        return self.profile.fixed_ms + jitter

    def apply(self) -> int:
        """Sleep for the next delay and return the injected milliseconds."""

        delay = self.delay_ms()
        if delay > 0:
            time.sleep(delay / 1000)
        return delay


class ChaosAgent:
    """Forward messages while emitting audit events for injected latency."""

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
        """Delay, publish the payload, and record the infrastructure event."""

        self._sequence += 1
        delay = self.latency.apply()
        event = ChaosEvent(
            kind=ChaosEventKind.LATENCY,
            topic=topic,
            delay_ms=delay,
            dropped=False,
            sequence=self._sequence,
        )
        self.bus.publish(self.topics.chaos_events(), event)
        self.bus.publish(topic, payload)
        return event

    def latency_hook(self, topic: str, _payload: dict[str, object]) -> None:
        """Hook compatible with MarketDataFeed latency injection."""

        self._sequence += 1
        delay = self.latency.apply()
        self.bus.publish(
            self.topics.chaos_events(),
            ChaosEvent(
                kind=ChaosEventKind.LATENCY,
                topic=topic,
                delay_ms=delay,
                dropped=False,
                sequence=self._sequence,
            ),
        )
