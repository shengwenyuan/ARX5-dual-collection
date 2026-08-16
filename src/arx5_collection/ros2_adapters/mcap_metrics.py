from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Callable

from arx5_collection.episode.models import StreamMetrics, StreamSpec


class HeaderTiming:
    def __init__(self) -> None:
        self.count = 0
        self.first_ns: int | None = None
        self.last_ns: int | None = None
        self.max_gap_ns = 0
        self.non_monotonic_count = 0

    def add(self, stamp_ns: int) -> None:
        if self.last_ns is not None:
            gap_ns = stamp_ns - self.last_ns
            if gap_ns <= 0:
                self.non_monotonic_count += 1
            else:
                self.max_gap_ns = max(self.max_gap_ns, gap_ns)
        self.first_ns = stamp_ns if self.first_ns is None else self.first_ns
        self.last_ns = stamp_ns
        self.count += 1

    def metrics(self, stream: StreamSpec, warning_ratio: float) -> StreamMetrics:
        duration_s = 0.0
        if self.first_ns is not None and self.last_ns is not None:
            duration_s = max(0.0, (self.last_ns - self.first_ns) / 1e9)
        observed_hz = (
            (self.count - 1) / duration_s
            if self.count > 1 and duration_s > 0
            else 0.0
        )
        warnings = []
        if self.count == 0:
            warnings.append("stream contains no recorded messages")
        elif self.count > 1 and observed_hz < stream.expected_hz * warning_ratio:
            warnings.append(
                f"observed frequency {observed_hz:.3f} Hz is below "
                f"{warning_ratio:.0%} of expected {stream.expected_hz:.3f} Hz"
            )
        if self.non_monotonic_count:
            warnings.append(
                f"{self.non_monotonic_count} non-monotonic Header intervals"
            )
        return StreamMetrics(
            id=stream.id,
            count=self.count,
            duration_s=duration_s,
            observed_hz=observed_hz,
            max_gap_ms=self.max_gap_ns / 1e6,
            warnings=tuple(warnings),
        )


def serialized_header_stamp_ns(payload: bytes) -> int:
    if len(payload) < 12:
        raise ValueError("serialized message is too short for std_msgs/Header")
    encapsulation = payload[:2]
    if encapsulation == b"\x00\x01":
        byte_order = "<"
    elif encapsulation == b"\x00\x00":
        byte_order = ">"
    else:
        raise ValueError(f"unsupported CDR encapsulation: {encapsulation.hex()}")
    sec, nanosec = struct.unpack_from(f"{byte_order}iI", payload, 4)
    if sec < 0 or nanosec >= 1_000_000_000:
        raise ValueError(f"invalid Header timestamp: sec={sec}, nanosec={nanosec}")
    return sec * 1_000_000_000 + nanosec


def _open_reader(mcap_path: Path) -> Any:
    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(mcap_path), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    return reader


def audit_mcap(
    mcap_path: Path,
    streams: tuple[StreamSpec, ...],
    warning_ratio: float = 0.9,
    reader_factory: Callable[[Path], Any] | None = None,
) -> tuple[StreamMetrics, ...]:
    if not mcap_path.is_file():
        raise FileNotFoundError(mcap_path)
    if not 0 < warning_ratio <= 1:
        raise ValueError("warning_ratio must be in (0, 1]")

    reader = (reader_factory or _open_reader)(mcap_path)
    available_topics = {item.name for item in reader.get_all_topics_and_types()}
    by_topic = {stream.topic: stream for stream in streams}
    timing = {stream.id: HeaderTiming() for stream in streams}

    while reader.has_next():
        topic, payload, _ = reader.read_next()
        stream = by_topic.get(topic)
        if stream is not None:
            timing[stream.id].add(serialized_header_stamp_ns(payload))

    results = []
    for stream in streams:
        metric = timing[stream.id].metrics(stream, warning_ratio)
        if stream.topic not in available_topics:
            metric = StreamMetrics(
                id=metric.id,
                count=metric.count,
                duration_s=metric.duration_s,
                observed_hz=metric.observed_hz,
                max_gap_ms=metric.max_gap_ms,
                warnings=(f"topic was not present in MCAP: {stream.topic}",),
            )
        results.append(metric)
    return tuple(results)
