"""Messaging abstraction for Solace and deterministic in-memory tests."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from pydantic import BaseModel

from common.config import Settings, get_settings

Topic: TypeAlias = str
Payload: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """Decoded broker message delivered to subscribers."""

    topic: Topic
    payload: Payload
    received_at_ns: int


MessageHandler = Callable[[MessageEnvelope], None]


class MessageBus(Protocol):
    """Minimal bus protocol used by services and tests."""

    def connect(self) -> None:
        """Open transport resources."""

    def publish(self, topic: Topic, payload: Mapping[str, Any] | BaseModel) -> None:
        """Publish one decoded JSON payload to a topic."""

    def subscribe(self, topic_filter: Topic, handler: MessageHandler) -> None:
        """Register a handler for messages matching the topic filter."""

    def close(self) -> None:
        """Release transport resources."""


def _json_payload(payload: Mapping[str, Any] | BaseModel) -> Payload:
    """Normalize payloads before broker encoding."""

    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return json.loads(json.dumps(dict(payload), default=str))


def topic_matches(topic_filter: Topic, topic: Topic) -> bool:
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
    """Synchronous broker for deterministic tests with reentrancy protection."""

    def __init__(self) -> None:
        self._subscriptions: list[tuple[Topic, MessageHandler]] = []
        self._lock = threading.RLock()
        self.published: list[MessageEnvelope] = []
        self._delivering = False
        self._pending: deque[MessageEnvelope] = deque()

    def connect(self) -> None:
        """No-op for API symmetry with SolaceClient."""

    def publish(self, topic: Topic, payload: Mapping[str, Any] | BaseModel) -> None:
        decoded = _json_payload(payload)
        envelope = MessageEnvelope(topic=topic, payload=decoded, received_at_ns=time.time_ns())
        with self._lock:
            self.published.append(envelope)
            subscriptions = list(self._subscriptions)

        if self._delivering:
            self._pending.append(envelope)
            return

        self._delivering = True
        try:
            self._dispatch(envelope, subscriptions)
            while self._pending:
                queued = self._pending.popleft()
                with self._lock:
                    subscriptions = list(self._subscriptions)
                self._dispatch(queued, subscriptions)
        finally:
            self._delivering = False

    def _dispatch(
        self, envelope: MessageEnvelope, subscriptions: list[tuple[Topic, MessageHandler]]
    ) -> None:
        for topic_filter, handler in subscriptions:
            if topic_matches(topic_filter, envelope.topic):
                handler(envelope)

    def subscribe(self, topic_filter: Topic, handler: MessageHandler) -> None:
        with self._lock:
            self._subscriptions.append((topic_filter, handler))

    def close(self) -> None:
        with self._lock:
            self._subscriptions.clear()


class SolaceClient:
    """Direct-message Solace client with an in-memory mode."""

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
        """Return the in-memory bus for publication inspection."""

        if self._memory is None:
            raise RuntimeError("memory_broker is only available when in_memory=True")
        return self._memory

    def connect(self) -> None:
        """Connect to Solace or initialize in-memory transport."""

        if self._connected:
            return
        if self._memory is not None:
            self._memory.connect()
            self._connected = True
            return

        try:
            from solace.messaging.messaging_service import MessagingService
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "solace-pubsubplus is required for broker mode; use in_memory=True for tests"
            ) from exc

        properties = {
            "solace.messaging.transport.host": self.settings.solace_host,
            "solace.messaging.service.vpn-name": self.settings.solace_vpn,
            "solace.messaging.authentication.scheme.basic.username": self.settings.solace_username,
            "solace.messaging.authentication.scheme.basic.password": self.settings.solace_password,
            "solace.messaging.client.name": self.settings.client_name,
            "solace.messaging.transport.reconnection-attempts": "3",
            "solace.messaging.transport.connection-retries": "3",
            "solace.messaging.transport.reconnection-attempts-wait-interval": "3000",
        }
        self._service = MessagingService.builder().from_properties(properties).build()
        self._service.connect()
        self._publisher = self._service.create_direct_message_publisher_builder().build()
        self._publisher.start()
        self._connected = True

    def publish(self, topic: Topic, payload: Mapping[str, Any] | BaseModel) -> None:
        """Publish JSON data to a Solace topic."""

        if not self._connected:
            self.connect()
        if self._memory is not None:
            self._memory.publish(topic, payload)
            return
        if self._service is None or self._publisher is None:
            raise RuntimeError("SolaceClient is not connected")

        from solace.messaging.resources.topic import Topic as SolaceTopic

        encoded = json.dumps(_json_payload(payload), separators=(",", ":"))
        outbound = self._service.message_builder().build(encoded)
        self._publisher.publish(outbound, SolaceTopic.of(topic))

    def subscribe(self, topic_filter: Topic, handler: MessageHandler) -> None:
        """Subscribe to decoded JSON payloads from a topic filter."""

        if not self._connected:
            self.connect()
        if self._memory is not None:
            self._memory.subscribe(topic_filter, handler)
            return
        if self._service is None:
            raise RuntimeError("SolaceClient is not connected")

        from solace.messaging.receiver.message_receiver import (
            MessageHandler as SolaceMessageHandler,
        )
        from solace.messaging.resources.topic_subscription import TopicSubscription

        receiver = (
            self._service.create_direct_message_receiver_builder()
            .with_subscriptions([TopicSubscription.of(topic_filter)])
            .build()
        )
        receiver.start()

        class _Adapter(SolaceMessageHandler):
            def on_message(self, message: Any) -> None:
                raw = message.get_payload_as_string()
                payload = json.loads(raw)
                destination = message.get_destination_name()
                handler(
                    MessageEnvelope(
                        topic=str(destination),
                        payload=payload,
                        received_at_ns=time.time_ns(),
                    )
                )

        receiver.receive_async(_Adapter())
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
