"""Chaos service entry point."""

from __future__ import annotations

import signal
import threading

from chaos.agent import ChaosAgent, LatencyInjector, LatencyProfile
from common.config import get_settings


def run() -> None:
    """Run a latency injector process."""

    settings = get_settings().with_client_name("chaos")
    agent = ChaosAgent(
        settings=settings,
        latency=LatencyInjector(LatencyProfile(fixed_ms=100, jitter_ms=25, seed=7)),
    )
    agent.bus.connect()

    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    shutdown.wait()
    agent.bus.close()


if __name__ == "__main__":
    run()
