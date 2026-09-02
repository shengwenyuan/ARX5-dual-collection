#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path
from typing import Any

from arx5_collection.collection.capture import CAPTURE_PROFILES, CaptureProfile

CAMERA_TOPICS = tuple(
    stream.topic
    for stream in CAPTURE_PROFILES[CaptureProfile.RGBD].streams
    if stream.message_type == "sensor_msgs/msg/Image"
)


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
            (self.count - 1) / duration_s if self.count > 1 and duration_s > 0 else 0.0
        )
        return {
            "count": self.count,
            "duration_s": duration_s,
            "observed_hz": observed_hz,
            "max_gap_ms": self.max_gap_ns / 1e6,
        }


def image_header_stamp_ns(payload: bytes) -> int:
    if len(payload) < 12:
        raise ValueError("serialized Image is too short")
    encapsulation = payload[:2]
    if encapsulation == b"\x00\x01":
        byte_order = "<"
    elif encapsulation == b"\x00\x00":
        byte_order = ">"
    else:
        raise ValueError(f"unsupported CDR encapsulation: {encapsulation.hex()}")
    sec, nanosec = struct.unpack_from(f"{byte_order}iI", payload, 4)
    if nanosec >= 1_000_000_000:
        raise ValueError(f"invalid Image header nanosecond value: {nanosec}")
    return sec * 1_000_000_000 + nanosec


def analyze(bag_path: Path) -> dict[str, Any]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_path), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    missing = sorted(set(CAMERA_TOPICS) - set(topic_types))
    if missing:
        raise RuntimeError(f"missing required camera topics: {missing}")
    wrong_types = {
        topic: topic_types[topic]
        for topic in CAMERA_TOPICS
        if topic_types[topic] != "sensor_msgs/msg/Image"
    }
    if wrong_types:
        raise RuntimeError(f"unexpected camera topic types: {wrong_types}")

    image_type = get_message("sensor_msgs/msg/Image")
    bag_timing = {topic: TimingStats() for topic in CAMERA_TOPICS}
    header_timing = {topic: TimingStats() for topic in CAMERA_TOPICS}
    samples: dict[str, dict[str, Any]] = {}

    while reader.has_next():
        topic, payload, bag_timestamp_ns = reader.read_next()
        if topic not in bag_timing:
            continue
        bag_timing[topic].add(bag_timestamp_ns)
        header_timing[topic].add(image_header_stamp_ns(payload))
        if topic not in samples:
            message = deserialize_message(payload, image_type)
            samples[topic] = {
                "width": int(message.width),
                "height": int(message.height),
                "encoding": message.encoding,
                "step": int(message.step),
                "frame_id": message.header.frame_id,
            }

    mcap_bytes = sum(path.stat().st_size for path in bag_path.glob("*.mcap"))
    return {
        "bag": str(bag_path),
        "mcap_bytes": mcap_bytes,
        "topics": {
            topic: {
                "type": topic_types[topic],
                "bag_timing": bag_timing[topic].summary(),
                "header_timing": header_timing[topic].summary(),
                "sample": samples.get(topic),
            }
            for topic in CAMERA_TOPICS
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
