"""Record broker messages as JSON Lines for deterministic replay."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TextIO

from common.config import Settings, Topics, get_settings
from common.models import RecordedMessage
from common.solace_client import MessageBus, MessageEnvelope, SolaceClient


class ReplayRecorder:
    """Subscribe to topics and persist every observed payload with timestamps."""

    def __init__(
        self,
        output_path: Path,
        bus: MessageBus | None = None,
        *,
        settings: Settings | None = None,
        topic_filter: str | None = None,
    ) -> None:
        self.output_path = output_path
        self.settings = (settings or get_settings()).normalized()
        self.topics = Topics(self.settings.topic_prefix)
        self.bus = bus or SolaceClient(self.settings)
        self.topic_filter = topic_filter or self.topics.all()
        self._sequence = 0
        self._file: TextIO | None = None

    def start(self) -> None:
        """Open the file and start recording matching broker messages."""

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.output_path.open("a", encoding="utf-8")
        self.bus.connect()
        self.bus.subscribe(self.topic_filter, self.record)

    def record(self, envelope: MessageEnvelope) -> RecordedMessage:
        """Write one envelope to disk and return the recorded representation."""

        self._sequence += 1
        message = RecordedMessage(
            sequence=self._sequence,
            topic=envelope.topic,
            payload=envelope.payload,
            timestamp_ns=envelope.received_at_ns or time.time_ns(),
        )
        if self._file is None:
            raise RuntimeError("ReplayRecorder.start() must be called before record()")
        self._file.write(message.to_json() + "\n")
        self._file.flush()
        return message

    def close(self) -> None:
        """Close the recording file."""

        if self._file is not None:
            self._file.close()
            self._file = None


def load_recording(path: Path) -> list[RecordedMessage]:
    """Load a JSONL recording into typed messages."""

    messages: list[RecordedMessage] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if stripped := line.strip():
                messages.append(RecordedMessage.model_validate(json.loads(stripped)))
    return messages
