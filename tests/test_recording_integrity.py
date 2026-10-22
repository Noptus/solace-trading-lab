"""Protect replay from corrupted order and recorder restart/concurrency bugs."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from common.config import Settings
from common.solace_client import InMemoryBroker, MessageEnvelope
from replay.player import ReplayPlayer
from replay.recorder import ReplayRecorder, load_recording


def envelope(n=0):
    return MessageEnvelope(topic="lab/event", payload={"n": n}, received_at_ns=n)


@pytest.mark.parametrize("sequences", [[2], [1, 1], [1, 3], [2, 1]])
def test_invalid_sequence_fails_before_any_publication(tmp_path, sequences):
    path = tmp_path / "recording.jsonl"
    path.write_text("".join(json.dumps(dict(sequence=n, topic="lab/event", payload={}, timestamp_ns=n)) + "\n"
                            for n in sequences))
    bus = InMemoryBroker()
    with pytest.raises(ValueError, match="expected sequence"):
        ReplayPlayer(path, bus).play()
    assert bus.published == []


def test_resume_and_closed_callback_do_not_duplicate_messages(tmp_path):
    path = tmp_path / "recording.jsonl"
    bus = InMemoryBroker()
    recorder = ReplayRecorder(path, bus, settings=Settings(in_memory_bus=True), topic_filter="lab/>")
    recorder.start()
    recorder.record(envelope())
    recorder.close()
    bus.publish("lab/event", {"ignored": True})
    recorder.start()
    bus.publish("lab/event", {"next": True})
    recorder.close()
    messages = load_recording(path)
    assert [m.sequence for m in messages] == [1, 2]
    assert messages[0].timestamp_ns == 0
    assert messages[1].payload == {"next": True}


def test_concurrent_record_calls_keep_contiguous_valid_jsonl(tmp_path):
    path = tmp_path / "recording.jsonl"
    recorder = ReplayRecorder(path, InMemoryBroker())
    recorder.start()
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda n: recorder.record(envelope(n)), range(200)))
    recorder.close()
    messages = load_recording(path)
    assert [m.sequence for m in messages] == list(range(1, 201))
    assert {m.payload["n"] for m in messages} == set(range(200))


def test_append_after_missing_final_newline(tmp_path):
    path = tmp_path / "recording.jsonl"
    first = ReplayRecorder(path, InMemoryBroker())
    first.start()
    first.record(envelope())
    first.close()
    path.write_text(path.read_text().rstrip("\n"))
    second = ReplayRecorder(path, InMemoryBroker())
    second.start()
    second.record(envelope(1))
    second.close()
    assert [m.sequence for m in load_recording(path)] == [1, 2]


def test_record_before_start_does_not_consume_sequence(tmp_path):
    path = tmp_path / "recording.jsonl"
    recorder = ReplayRecorder(path, InMemoryBroker())
    with pytest.raises(RuntimeError):
        recorder.record(envelope())
    recorder.start()
    assert recorder.record(envelope()).sequence == 1
    with pytest.raises(RuntimeError, match="already started"):
        recorder.start()
    recorder.close()
