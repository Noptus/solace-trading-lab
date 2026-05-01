"""Chaos demo entry point."""

from __future__ import annotations

import time

from chaos.agent import ChaosAgent, LatencyInjector, LatencyProfile
from common.config import get_settings


def run() -> None:
    """Start a lightweight chaos agent that can be imported by demo services."""

    settings = get_settings()
    agent = ChaosAgent(
        settings=settings,
        latency=LatencyInjector(LatencyProfile(fixed_ms=100, jitter_ms=25, seed=7)),
    )
    agent.bus.connect()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    run()
