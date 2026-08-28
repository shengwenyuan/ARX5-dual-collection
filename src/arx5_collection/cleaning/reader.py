from __future__ import annotations

import json
import math
from pathlib import Path
import struct
from typing import Any

from arx5_collection.capture import profile_from_metadata
from arx5_collection.capture import stream_contract
from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import LEFT_ARM_TOPIC
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.cleaning.models import RIGHT_ARM_TOPIC
from arx5_collection.cleaning.models import required_topics


IMAGE_TYPE = "sensor_msgs/msg/Image"
ARM_TYPE = "arx5_collection_interfaces/msg/ArmState"


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


def load_metadata(episode_dir: Path) -> dict[str, Any]:
    path = episode_dir / "metadata.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open() as stream:
        metadata = json.load(stream)
    if metadata.get("episode_id") != episode_dir.name:
        raise ValueError("metadata episode_id does not match the episode directory")
    return metadata


def _expected_type(topic: str) -> str:
    return ARM_TYPE if topic in (LEFT_ARM_TOPIC, RIGHT_ARM_TOPIC) else IMAGE_TYPE


def read_episode_scan(episode_dir: Path) -> EpisodeScan:
    """Read the profile's required streams without retaining image payloads.

    ROS imports stay inside this adapter so the pure cleaning package remains importable
    in development and unit-test environments without ROS installed.
    """

    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    episode_dir = episode_dir.resolve()
    mcap_path = episode_dir / "episode.mcap"
    if not mcap_path.is_file():
        raise FileNotFoundError(mcap_path)
    metadata = load_metadata(episode_dir)
    profile = profile_from_metadata(metadata)
    expected_streams = stream_contract(profile)
    expected_topics = required_topics(profile)
    metadata_streams = metadata.get("streams")
    actual_streams = {
        item.get("id"): item.get("topic")
        for item in metadata_streams
        if isinstance(item, dict)
    } if isinstance(metadata_streams, list) else {}
    if actual_streams != expected_streams:
        raise ValueError(
            f"metadata streams do not match capture profile {profile.value}"
        )

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(mcap_path), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    missing = sorted(set(expected_topics) - set(topic_types))
    if missing:
        raise ValueError(f"missing required topics: {missing}")
    wrong_types = {
        topic: {"expected": _expected_type(topic), "actual": topic_types[topic]}
        for topic in expected_topics
        if topic_types[topic] != _expected_type(topic)
    }
    if wrong_types:
        raise ValueError(f"unexpected topic types: {wrong_types}")

    arm_message_type = get_message(ARM_TYPE)
    refs: dict[str, list[MessageRef]] = {topic: [] for topic in expected_topics}
    left_arm: list[ArmSample] = []
    right_arm: list[ArmSample] = []
    sequences = {topic: 0 for topic in expected_topics}

    while reader.has_next():
        topic, payload, bag_timestamp_ns = reader.read_next()
        if topic not in refs:
            continue
        ref = MessageRef(
            topic=topic,
            sequence=sequences[topic],
            header_stamp_ns=serialized_header_stamp_ns(payload),
            bag_timestamp_ns=int(bag_timestamp_ns),
        )
        sequences[topic] += 1
        refs[topic].append(ref)
        if topic not in (LEFT_ARM_TOPIC, RIGHT_ARM_TOPIC):
            continue
        message = deserialize_message(payload, arm_message_type)
        eef = tuple(float(value) for value in message.eef_xyzrpy)
        joints = tuple(float(value) for value in message.joint_positions)
        gripper = float(message.gripper_position)
        if not all(math.isfinite(value) for value in (*eef, *joints, gripper)):
            continue
        sample = ArmSample(
            ref=ref,
            joint_positions=joints,
            gripper_position=gripper,
            eef_xyzrpy=eef,
        )
        (left_arm if topic == LEFT_ARM_TOPIC else right_arm).append(sample)

    return EpisodeScan(
        episode_dir=episode_dir,
        refs_by_topic={topic: tuple(items) for topic, items in refs.items()},
        left_arm=tuple(left_arm),
        right_arm=tuple(right_arm),
        topic_types=topic_types,
        capture_profile=profile,
    )
