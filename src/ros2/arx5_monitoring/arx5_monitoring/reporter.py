from __future__ import annotations

from time import monotonic
from typing import Any

from arx5_collection_interfaces.msg import StreamStatus

from arx5_monitoring.tracker import StreamTracker


class StreamStatusReporter:
    def __init__(
        self,
        node: Any,
        publisher: Any,
        stream_id: str,
        topic: str,
    ) -> None:
        if not stream_id or not topic:
            raise ValueError("stream_id and topic must not be empty")
        self.node = node
        self.publisher = publisher
        self.stream_id = stream_id
        self.topic = topic
        self.tracker = StreamTracker()

    def observe(self, message_stamp_ns: int, arrival_s: float | None = None) -> None:
        self.tracker.observe(
            message_stamp_ns,
            monotonic() if arrival_s is None else arrival_s,
        )

    def publish(self, now_s: float | None = None) -> None:
        snapshot = self.tracker.snapshot(
            monotonic() if now_s is None else now_s,
            reset_window=True,
        )
        message = StreamStatus()
        message.header.stamp = self.node.get_clock().now().to_msg()
        message.stream_id = self.stream_id
        message.topic = self.topic
        message.total_count = snapshot.total_count
        message.window_count = snapshot.window_count
        message.window_duration_s = snapshot.window_duration_s
        message.observed_hz = snapshot.observed_hz
        message.max_gap_ms = snapshot.max_gap_ms
        if snapshot.last_message_stamp_ns is not None:
            message.last_message_stamp.sec = (
                snapshot.last_message_stamp_ns // 1_000_000_000
            )
            message.last_message_stamp.nanosec = (
                snapshot.last_message_stamp_ns % 1_000_000_000
            )
        message.silence_s = snapshot.silence_s
        message.non_monotonic_count = snapshot.non_monotonic_count
        self.publisher.publish(message)
