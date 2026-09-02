#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from itertools import zip_longest
from pathlib import Path
from typing import Any, Iterator

from arx5_collection.collection.capture import RGBD_STREAMS
from arx5_collection.collection.runtime.profiles import TEACHING_ARM_PROFILE

TOPIC_PAIRS = (
    (TEACHING_ARM_PROFILE.left_input_topic, RGBD_STREAMS["left_arm_state"]),
    (TEACHING_ARM_PROFILE.right_input_topic, RGBD_STREAMS["right_arm_state"]),
)


def _messages(bag: Path, topic: str) -> Iterator[Any]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    if topic not in topic_types:
        raise RuntimeError(f"{bag} does not contain {topic}")
    message_type = get_message(topic_types[topic])
    reader.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    while reader.has_next():
        _, payload, _ = reader.read_next()
        yield deserialize_message(payload, message_type)


def _header(message: Any) -> tuple[int, int, str]:
    return (
        int(message.header.stamp.sec),
        int(message.header.stamp.nanosec),
        str(message.header.frame_id),
    )


def _source_values(message: Any) -> tuple[Any, ...]:
    return (
        tuple(message.end_pos),
        tuple(message.joint_pos[:6]),
        tuple(message.joint_vel[:6]),
        tuple(message.joint_cur[:6]),
        float(message.joint_pos[6]),
        float(message.joint_vel[6]),
        float(message.joint_cur[6]),
    )


def _logical_values(message: Any) -> tuple[Any, ...]:
    return (
        tuple(message.eef_xyzrpy),
        tuple(message.joint_positions),
        tuple(message.joint_velocities),
        tuple(message.joint_currents),
        float(message.gripper_position),
        float(message.gripper_velocity),
        float(message.gripper_current),
    )


def _compare_topic(
    input_bag: Path, output_bag: Path, source: str, logical: str
) -> dict[str, Any]:
    sentinel = object()
    count = 0
    first_header_ns: int | None = None
    last_header_ns: int | None = None
    max_gap_ns = 0
    for index, (source_message, logical_message) in enumerate(
        zip_longest(
            _messages(input_bag, source),
            _messages(output_bag, logical),
            fillvalue=sentinel,
        )
    ):
        if source_message is sentinel or logical_message is sentinel:
            raise RuntimeError(
                f"message count differs for {source} -> {logical} at index {index}"
            )
        if _header(source_message) != _header(logical_message):
            raise RuntimeError(
                f"Header differs for {source} -> {logical} at index {index}"
            )
        if _source_values(source_message) != _logical_values(logical_message):
            raise RuntimeError(
                f"fields differ for {source} -> {logical} at index {index}"
            )

        sec, nanosec, _ = _header(logical_message)
        header_ns = sec * 1_000_000_000 + nanosec
        if last_header_ns is not None:
            max_gap_ns = max(max_gap_ns, header_ns - last_header_ns)
        first_header_ns = header_ns if first_header_ns is None else first_header_ns
        last_header_ns = header_ns
        count += 1

    duration_s = 0.0
    if first_header_ns is not None and last_header_ns is not None:
        duration_s = (last_header_ns - first_header_ns) / 1e9
    observed_hz = (count - 1) / duration_s if count > 1 and duration_s > 0 else 0.0
    return {
        "source_topic": source,
        "logical_topic": logical,
        "count": count,
        "header_duration_s": duration_s,
        "observed_hz": observed_hz,
        "max_header_gap_ms": max_gap_ns / 1e6,
        "headers_equal": True,
        "fields_equal": True,
    }


def compare(input_bag: Path, output_bag: Path) -> dict[str, Any]:
    return {
        "input_bag": str(input_bag),
        "output_bag": str(output_bag),
        "topics": {
            logical: _compare_topic(input_bag, output_bag, source, logical)
            for source, logical in TOPIC_PAIRS
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_bag", type=Path)
    parser.add_argument("output_bag", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = compare(args.input_bag, args.output_bag)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")


if __name__ == "__main__":
    main()
