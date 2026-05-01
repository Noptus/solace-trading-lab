"""Replay recorded messages into a broker."""

from __future__ import annotations

import time
from pathlib import Path

from common.config import Settings, get_settings
from common.models import RecordedMessage
from common.solace_client import MessageBus, SolaceClient
from replay.recorder import load_recording


class ReplayPlayer:
    """Publish a JSONL recording with deterministic ordering."""

    def __init__(
        self,
        input_path: Path,
        bus: MessageBus | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.input_path = input_path
        self.settings = (settings or get_settings()).normalized()
        self.bus = bus or SolaceClient(self.settings)

    def load(self) -> list[RecordedMessage]:
        """Load and sequence-sort the recording before playback."""

        return sorted(load_recording(self.input_path), key=lambda message: message.sequence)

    def play(self, *, realtime: bool = False, speed: float = 1.0) -> list[RecordedMessage]:
        """Replay all messages.

        `realtime=False` is the deterministic default for tests and what-if
        experiments.  Realtime playback is available for demos that want human
        observable delays.
        """

        if speed <= 0:
            raise ValueError("speed must be positive")
        messages = self.load()
        self.bus.connect()
        previous_timestamp: int | None = None
        for message in messages:
            if realtime and previous_timestamp is not None:
                delay_ns = max(0, message.timestamp_ns - previous_timestamp)
                time.sleep(delay_ns / 1_000_000_000 / speed)
            self.bus.publish(message.topic, message.payload)
            previous_timestamp = message.timestamp_ns
        return messages
