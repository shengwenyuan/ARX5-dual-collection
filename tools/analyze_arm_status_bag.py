#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


ARM_TOPICS = ("/arm_master_l_status", "/arm_master_r_status")
VECTOR_FIELDS = ("end_pos", "joint_pos", "joint_vel", "joint_cur")


class TimingStats:
    def __init__(self) -> None:
        self.count = 0
        self.first_ns: int | None = None
        self.last_ns: int | None = None
        self.max_gap_ns = 0

    def add(self, timestamp_ns: int) -> None:
        if self.last_ns is not None:
            self.max_gap_ns = max(self.max_gap_ns, timestamp_ns - self.last_ns)
        self.first_ns = timestamp_ns if self.first_ns is None else self.first_ns
        self.last_ns = timestamp_ns
        self.count += 1

    def summary(self) -> dict[str, float | int]:
        duration_s = 0.0
        if self.first_ns is not None and self.last_ns is not None:
            duration_s = (self.last_ns - self.first_ns) / 1e9
        observed_hz = (
            (self.count - 1) / duration_s
            if self.count > 1 and duration_s > 0
            else 0.0
        )
        return {
            "count": self.count,
            "duration_s": duration_s,
            "observed_hz": observed_hz,
            "max_gap_ms": self.max_gap_ns / 1e6,
        }


class VectorStats:
    def __init__(self) -> None:
        self.minimum: list[float] | None = None
        self.maximum: list[float] | None = None
        self.nonfinite_count = 0

    def add(self, values: Any) -> None:
        vector = [float(value) for value in values]
        if not all(math.isfinite(value) for value in vector):
            self.nonfinite_count += 1
            return
        if self.minimum is None:
            self.minimum = vector.copy()
            self.maximum = vector.copy()
            return
        assert self.maximum is not None
        if len(vector) != len(self.minimum):
            raise RuntimeError("vector length changed during recording")
        self.minimum = [min(old, value) for old, value in zip(self.minimum, vector)]
        self.maximum = [max(old, value) for old, value in zip(self.maximum, vector)]

    def summary(self) -> dict[str, Any]:
        minimum = self.minimum or []
        maximum = self.maximum or []
        return {
            "min": minimum,
            "max": maximum,
            "range": [high - low for low, high in zip(minimum, maximum)],
            "nonfinite_count": self.nonfinite_count,
        }


def analyze(bag_path: Path) -> dict[str, Any]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_path), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {
        item.name: item.type for item in reader.get_all_topics_and_types()
    }
    missing = sorted(set(ARM_TOPICS) - set(topic_types))
    if missing:
        raise RuntimeError(f"missing required arm topics: {missing}")

    message_types = {topic: get_message(topic_types[topic]) for topic in ARM_TOPICS}
    bag_timing = {topic: TimingStats() for topic in ARM_TOPICS}
    header_timing = {topic: TimingStats() for topic in ARM_TOPICS}
    vectors = {
        topic: {field: VectorStats() for field in VECTOR_FIELDS}
        for topic in ARM_TOPICS
    }

    while reader.has_next():
        topic, payload, bag_timestamp_ns = reader.read_next()
        if topic not in message_types:
            continue
        message = deserialize_message(payload, message_types[topic])
        bag_timing[topic].add(bag_timestamp_ns)
        header_timestamp_ns = (
            int(message.header.stamp.sec) * 1_000_000_000
            + int(message.header.stamp.nanosec)
        )
        header_timing[topic].add(header_timestamp_ns)
        for field in VECTOR_FIELDS:
            vectors[topic][field].add(getattr(message, field))

    return {
        "bag": str(bag_path),
        "topics": {
            topic: {
                "type": topic_types[topic],
                "bag_timing": bag_timing[topic].summary(),
                "header_timing": header_timing[topic].summary(),
                "fields": {
                    field: vectors[topic][field].summary()
                    for field in VECTOR_FIELDS
                },
            }
            for topic in ARM_TOPICS
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bag", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(args.bag)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")


if __name__ == "__main__":
    main()
