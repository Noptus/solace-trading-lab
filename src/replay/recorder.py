"""Record broker messages as JSON Lines for deterministic replay."""

from __future__ import annotations

import json
import threading
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
        self._lock = threading.RLock()
        self._subscribed = False

    def start(self) -> None:
        """Open the file and start recording matching broker messages."""

        with self._lock:
            if self._file is not None:
                raise RuntimeError("recorder is already started")
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            previous = load_recording(self.output_path) if self.output_path.exists() else []
            self._sequence = previous[-1].sequence if previous else 0
            needs_newline = False
            if self.output_path.exists() and self.output_path.stat().st_size:
                with self.output_path.open("rb") as existing:
                    existing.seek(-1, 2)
                    needs_newline = existing.read(1) != b"\n"
            self._file = self.output_path.open("a", encoding="utf-8")
            if needs_newline:
                self._file.write("\n")
            try:
                self.bus.connect()
                if not self._subscribed:
                    self.bus.subscribe(self.topic_filter, self._on_message)
                    self._subscribed = True
            except Exception:
                self.close()
                raise

    def _on_message(self, envelope: MessageEnvelope) -> None:
        """Ignore transport callbacks while the recorder is closed."""
        with self._lock:
            if self._file is not None:
                self.record(envelope)

    def record(self, envelope: MessageEnvelope) -> RecordedMessage:
        """Serialize sequence assignment and file writes within this process."""
        with self._lock:
            if self._file is None:
                raise RuntimeError("ReplayRecorder.start() must be called before record()")
            message = RecordedMessage(
                sequence=self._sequence + 1,
                topic=envelope.topic,
                payload=envelope.payload,
                timestamp_ns=envelope.received_at_ns,
            )
            self._file.write(message.to_json() + "\n")
            self._file.flush()
            self._sequence = message.sequence
            return message

    def close(self) -> None:
        """Stop file writes without closing a possibly shared message bus."""
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def load_recording(path: Path) -> list[RecordedMessage]:
    """Validate a complete stream: contiguous sequences starting at one."""

    messages: list[RecordedMessage] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if stripped := line.strip():
                message = RecordedMessage.model_validate(json.loads(stripped))
                expected = len(messages) + 1
                if message.sequence != expected:
                    raise ValueError(
                        f"{path}:{line_number}: expected sequence {expected}, got {message.sequence}"
                    )
                messages.append(message)
    return messages
