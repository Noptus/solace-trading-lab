"""Small messaging abstraction around Solace PubSub+.

The exchange and strategies depend on this wrapper instead of the Solace SDK
directly.  That keeps the deterministic core testable with the in-memory bus
while preserving the same publish/subscribe shape used by the real broker.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from common.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """A decoded broker message delivered to subscribers."""

    topic: str
    payload: dict[str, Any]
    received_at_ns: int


MessageHandler = Callable[[MessageEnvelope], None]


class MessageBus(Protocol):
    """Minimal bus protocol used by services and tests."""

    def connect(self) -> None:
        """Open underlying resources."""

    def publish(self, topic: str, payload: Mapping[str, Any] | BaseModel) -> None:
        """Publish one decoded JSON payload to a topic."""

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        """Register a handler for messages matching the topic filter."""

    def close(self) -> None:
        """Release underlying resources."""


def _json_payload(payload: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """Normalize model or mapping payloads before broker encoding."""

    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return json.loads(json.dumps(dict(payload), default=str))


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Match Solace-style topic filters with `*` and `>` wildcards."""

    filter_parts = topic_filter.strip("/").split("/")
    topic_parts = topic.strip("/").split("/")
    for index, part in enumerate(filter_parts):
        if part == ">":
            return True
        if index >= len(topic_parts):
            return False
        if part != "*" and part != topic_parts[index]:
            return False
    return len(filter_parts) == len(topic_parts)


class InMemoryBroker:
    """Synchronous deterministic broker used by tests and single-process demos."""

    def __init__(self) -> None:
        self._subscriptions: list[tuple[str, MessageHandler]] = []
        self._lock = threading.RLock()
        self.published: list[MessageEnvelope] = []

    def connect(self) -> None:
        """No-op for API symmetry with SolaceClient."""

    def publish(self, topic: str, payload: Mapping[str, Any] | BaseModel) -> None:
        decoded = _json_payload(payload)
        envelope = MessageEnvelope(topic=topic, payload=decoded, received_at_ns=time.time_ns())
        with self._lock:
            self.published.append(envelope)
            subscriptions = list(self._subscriptions)
        for topic_filter, handler in subscriptions:
            if topic_matches(topic_filter, topic):
                handler(envelope)

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        with self._lock:
            self._subscriptions.append((topic_filter, handler))

    def close(self) -> None:
        with self._lock:
            self._subscriptions.clear()


class SolaceClient:
    """Direct-message Solace client with an in-memory fallback for deterministic runs."""

    def __init__(self, settings: Settings | None = None, *, in_memory: bool | None = None) -> None:
        self.settings = settings or get_settings()
        self.in_memory = self.settings.in_memory_bus if in_memory is None else in_memory
        self._memory = InMemoryBroker() if self.in_memory else None
        self._service: Any | None = None
        self._publisher: Any | None = None
        self._receivers: list[Any] = []
        self._connected = False

    @property
    def memory_broker(self) -> InMemoryBroker:
        """Expose the in-memory bus for tests that need to inspect publications."""

        if self._memory is None:
            raise RuntimeError("memory_broker is only available when in_memory=True")
        return self._memory

    def connect(self) -> None:
        """Connect to Solace or initialize the deterministic in-memory transport."""

        if self._connected:
            return
        if self._memory is not None:
            self._memory.connect()
            self._connected = True
            return

        try:
            from solace.messaging.messaging_service import MessagingService
        except ImportError as exc:  # pragma: no cover - exercised only without dependency installed.
            raise RuntimeError(
                "solace-pubsubplus is required for broker mode; use in_memory=True for tests"
            ) from exc

        properties = {
            "solace.messaging.transport.host": self.settings.solace_host,
            "solace.messaging.service.vpn-name": self.settings.solace_vpn,
            "solace.messaging.authentication.scheme.basic.username": self.settings.solace_username,
            "solace.messaging.authentication.scheme.basic.password": self.settings.solace_password,
            "solace.messaging.client.name": self.settings.client_name,
        }
        self._service = MessagingService.builder().from_properties(properties).build()
        self._service.connect()
        self._publisher = self._service.create_direct_message_publisher_builder().build()
        self._publisher.start()
        self._connected = True

    def publish(self, topic: str, payload: Mapping[str, Any] | BaseModel) -> None:
        """Publish JSON data to a Solace topic."""

        if not self._connected:
            self.connect()
        if self._memory is not None:
            self._memory.publish(topic, payload)
            return
        if self._service is None or self._publisher is None:
            raise RuntimeError("SolaceClient is not connected")

        from solace.messaging.resources.topic import Topic

        encoded = json.dumps(_json_payload(payload), separators=(",", ":")).encode("utf-8")
        outbound = self._service.message_builder().build(encoded)
        self._publisher.publish(outbound, Topic.of(topic))

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        """Subscribe to decoded JSON payloads from a topic filter."""

        if not self._connected:
            self.connect()
        if self._memory is not None:
            self._memory.subscribe(topic_filter, handler)
            return
        if self._service is None:
            raise RuntimeError("SolaceClient is not connected")

        from solace.messaging.resources.topic_subscription import TopicSubscription

        receiver = (
            self._service.create_direct_message_receiver_builder()
            .with_subscriptions([TopicSubscription.of(topic_filter)])
            .build()
        )
        receiver.start()

        def _on_message(message: Any) -> None:
            raw_payload = message.get_payload_as_bytes()
            payload = json.loads(raw_payload.decode("utf-8"))
            destination = getattr(message, "get_destination_name", lambda: topic_filter)()
            handler(
                MessageEnvelope(
                    topic=str(destination),
                    payload=payload,
                    received_at_ns=time.time_ns(),
                )
            )

        receiver.receive_async(_on_message)
        self._receivers.append(receiver)

    def close(self) -> None:
        """Stop receivers and close the underlying messaging service."""

        if self._memory is not None:
            self._memory.close()
            self._connected = False
            return
        for receiver in self._receivers:
            try:
                receiver.terminate()
            except Exception:
                pass
        self._receivers.clear()
        if self._publisher is not None:
            try:
                self._publisher.terminate()
            except Exception:
                pass
        if self._service is not None:
            try:
                self._service.disconnect()
            except Exception:
                pass
        self._connected = False
