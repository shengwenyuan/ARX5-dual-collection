#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StreamDefinition:
    id: str
    topic: str
    message_type: str
    expected_hz: float


CAMERA_STREAMS = tuple(
    StreamDefinition(
        id=f"camera_{role}_{leaf}",
        topic=f"/sensors/camera_{role}/{topic}",
        message_type="sensor_msgs/msg/Image",
        expected_hz=30.0,
    )
    for role in ("left", "right", "overview")
    for leaf, topic in (
        ("color", "color/image_raw"),
        ("aligned_depth", "aligned_depth/image_raw"),
    )
)
ARM_STREAMS = (
    StreamDefinition(
        id="left_arm_state",
        topic="/embodiments/left_arm/state",
        message_type="arx5_collection_interfaces/msg/ArmState",
        expected_hz=1000.0,
    ),
    StreamDefinition(
        id="right_arm_state",
        topic="/embodiments/right_arm/state",
        message_type="arx5_collection_interfaces/msg/ArmState",
        expected_hz=1000.0,
    ),
)
PROFILES = {
    "cameras": CAMERA_STREAMS,
    "arms": ARM_STREAMS,
    "episode": CAMERA_STREAMS + ARM_STREAMS,
}
STATUS_TOPIC = "/monitoring/stream_status"
STATUS_TYPE = "arx5_collection_interfaces/msg/StreamStatus"


class TimingStats:
    def __init__(self) -> None:
        self.count = 0
        self.first_ns: int | None = None
        self.last_ns: int | None = None
        self.max_gap_ns = 0
        self.non_monotonic_count = 0

    def add(self, timestamp_ns: int) -> None:
        if self.last_ns is not None:
            gap_ns = timestamp_ns - self.last_ns
            if gap_ns <= 0:
                self.non_monotonic_count += 1
            else:
                self.max_gap_ns = max(self.max_gap_ns, gap_ns)
        self.first_ns = timestamp_ns if self.first_ns is None else self.first_ns
        self.last_ns = timestamp_ns
        self.count += 1

    def summary(self, expected_hz: float, warning_ratio: float) -> dict[str, Any]:
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
            warnings.append("stream contains no messages")
        elif self.count > 1 and observed_hz < expected_hz * warning_ratio:
            warnings.append(
                f"observed frequency {observed_hz:.3f} Hz is below "
                f"{warning_ratio:.0%} of expected {expected_hz:.3f} Hz"
            )
        if self.non_monotonic_count:
            warnings.append(
                f"{self.non_monotonic_count} non-monotonic Header intervals"
            )
        return {
            "count": self.count,
            "duration_s": duration_s,
            "observed_hz": observed_hz,
            "max_gap_ms": self.max_gap_ns / 1e6,
            "non_monotonic_count": self.non_monotonic_count,
            "warnings": warnings,
        }


class TelemetryStats:
    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.updates = 0
        self.first_total_count: int | None = None
        self.last_total_count = 0
        self.frequency_samples: list[float] = []
        self.max_gap_ms = 0.0
        self.max_silence_s = 0.0
        self.non_monotonic_count = 0

    def add(
        self,
        topic: str,
        total_count: int,
        window_count: int,
        observed_hz: float,
        max_gap_ms: float,
        silence_s: float,
        non_monotonic_count: int,
    ) -> None:
        if topic != self.topic:
            raise RuntimeError(f"telemetry topic changed from {self.topic} to {topic}")
        if total_count < self.last_total_count:
            raise RuntimeError("telemetry total_count decreased")
        if non_monotonic_count < self.non_monotonic_count:
            raise RuntimeError("telemetry non_monotonic_count decreased")
        if self.first_total_count is None:
            self.first_total_count = total_count
        self.updates += 1
        self.last_total_count = total_count
        if window_count > 1 and observed_hz > 0:
            self.frequency_samples.append(observed_hz)
        self.max_gap_ms = max(self.max_gap_ms, max_gap_ms)
        self.max_silence_s = max(self.max_silence_s, silence_s)
        self.non_monotonic_count = non_monotonic_count

    def summary(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "updates": self.updates,
            "first_total_count": self.first_total_count or 0,
            "last_total_count": self.last_total_count,
            "min_observed_hz": min(self.frequency_samples, default=0.0),
            "max_observed_hz": max(self.frequency_samples, default=0.0),
            "max_gap_ms": self.max_gap_ms,
            "max_silence_s": self.max_silence_s,
            "non_monotonic_count": self.non_monotonic_count,
        }


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


def rgbd_pairing(
    header_stamps: dict[str, set[int]],
) -> dict[str, dict[str, int]]:
    result = {}
    for role in ("left", "right", "overview"):
        color = header_stamps.get(f"camera_{role}_color", set())
        depth = header_stamps.get(f"camera_{role}_aligned_depth", set())
        result[role] = {
            "paired_count": len(color & depth),
            "color_only_count": len(color - depth),
            "depth_only_count": len(depth - color),
        }
    return result


def analyze(
    bag_path: Path,
    streams: tuple[StreamDefinition, ...],
    warning_ratio: float = 0.9,
    require_telemetry: bool = False,
    telemetry_only: bool = False,
) -> dict[str, Any]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    if not 0 < warning_ratio <= 1:
        raise ValueError("warning_ratio must be in (0, 1]")
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_path), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {
        item.name: item.type for item in reader.get_all_topics_and_types()
    }
    by_topic = {stream.topic: stream for stream in streams}
    if not telemetry_only:
        missing = sorted(set(by_topic) - set(topic_types))
        if missing:
            raise RuntimeError(f"missing required stream topics: {missing}")
        wrong_types = {
            topic: topic_types[topic]
            for topic, stream in by_topic.items()
            if topic_types[topic] != stream.message_type
        }
        if wrong_types:
            raise RuntimeError(f"unexpected stream topic types: {wrong_types}")
    if STATUS_TOPIC in topic_types and topic_types[STATUS_TOPIC] != STATUS_TYPE:
        raise RuntimeError(
            f"unexpected telemetry topic type: {topic_types[STATUS_TOPIC]}"
        )
    if require_telemetry and STATUS_TOPIC not in topic_types:
        raise RuntimeError(f"missing required telemetry topic: {STATUS_TOPIC}")

    timing = {stream.id: TimingStats() for stream in streams}
    header_stamps = {stream.id: set() for stream in streams}
    telemetry: dict[str, TelemetryStats] = {}
    status_message_type = (
        get_message(STATUS_TYPE) if STATUS_TOPIC in topic_types else None
    )
    while reader.has_next():
        topic, payload, _ = reader.read_next()
        stream = by_topic.get(topic)
        if stream is not None:
            stamp_ns = serialized_header_stamp_ns(payload)
            timing[stream.id].add(stamp_ns)
            header_stamps[stream.id].add(stamp_ns)
        elif topic == STATUS_TOPIC and status_message_type is not None:
            message = deserialize_message(payload, status_message_type)
            stats = telemetry.setdefault(
                message.stream_id,
                TelemetryStats(message.topic),
            )
            stats.add(
                topic=message.topic,
                total_count=int(message.total_count),
                window_count=int(message.window_count),
                observed_hz=float(message.observed_hz),
                max_gap_ms=float(message.max_gap_ms),
                silence_s=float(message.silence_s),
                non_monotonic_count=int(message.non_monotonic_count),
            )

    expected_ids = {stream.id for stream in streams}
    if require_telemetry:
        missing_ids = sorted(expected_ids - set(telemetry))
        if missing_ids:
            raise RuntimeError(f"missing required telemetry stream ids: {missing_ids}")

    return {
        "bag": str(bag_path),
        "warning_ratio": warning_ratio,
        "streams": {} if telemetry_only else {
            stream.id: {
                "topic": stream.topic,
                "type": stream.message_type,
                "expected_hz": stream.expected_hz,
                **timing[stream.id].summary(stream.expected_hz, warning_ratio),
            }
            for stream in streams
        },
        "rgbd_pairing": (
            {}
            if telemetry_only
            else rgbd_pairing(header_stamps)
        ),
        "telemetry": {
            stream_id: stats.summary()
            for stream_id, stats in sorted(telemetry.items())
            if stream_id in expected_ids
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bag", type=Path)
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="episode",
    )
    parser.add_argument("--warning-ratio", type=float, default=0.9)
    parser.add_argument("--require-telemetry", action="store_true")
    parser.add_argument("--telemetry-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(
        args.bag,
        PROFILES[args.profile],
        warning_ratio=args.warning_ratio,
        require_telemetry=args.require_telemetry,
        telemetry_only=args.telemetry_only,
    )
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")


if __name__ == "__main__":
    main()
