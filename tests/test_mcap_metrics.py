from __future__ import annotations

import struct

from arx5_collection.collection.episode.models import StreamSpec
from arx5_collection.adapters.ros2.mcap_metrics import audit_mcap


def payload(stamp_ns: int) -> bytes:
    sec, nanosec = divmod(stamp_ns, 1_000_000_000)
    return b"\x00\x01\x00\x00" + struct.pack("<iI", sec, nanosec)


class FakeReader:
    def __init__(self, topics, messages) -> None:
        self.topics = [type("Topic", (), {"name": topic}) for topic in topics]
        self.messages = iter(messages)
        self.next_message = None

    def get_all_topics_and_types(self):
        return self.topics

    def has_next(self):
        if self.next_message is None:
            try:
                self.next_message = next(self.messages)
            except StopIteration:
                return False
        return True

    def read_next(self):
        result = self.next_message
        self.next_message = None
        return result


def test_audit_returns_episode_stream_metrics(tmp_path) -> None:
    mcap = tmp_path / "episode.mcap"
    mcap.write_bytes(b"mcap")
    streams = (StreamSpec("camera", "/camera", True, 30.0),)
    reader = FakeReader(
        ("/camera",),
        (
            ("/camera", payload(1_000_000_000), 0),
            ("/camera", payload(1_033_000_000), 0),
            ("/camera", payload(1_067_000_000), 0),
        ),
    )

    metrics = audit_mcap(mcap, streams, reader_factory=lambda path: reader)

    assert metrics[0].count == 3
    assert metrics[0].observed_hz == 2 / 0.067
    assert metrics[0].max_gap_ms == 34.0
    assert metrics[0].warnings == ()


def test_audit_reports_missing_optional_topic_without_failing(tmp_path) -> None:
    mcap = tmp_path / "episode.mcap"
    mcap.write_bytes(b"mcap")
    streams = (StreamSpec("optional", "/optional", False, 10.0),)
    reader = FakeReader((), ())
    metrics = audit_mcap(mcap, streams, reader_factory=lambda path: reader)
    assert metrics[0].count == 0
    assert "not present" in metrics[0].warnings[0]
