from __future__ import annotations

from pathlib import Path

from .models import AUTHORITY_TOPIC
from .models import AUTHORITY_TYPE
from .models import AuthorityEventRecord
from .models import AuthorityEventType


def read_authority_events(episode_dir: Path) -> tuple[AuthorityEventRecord, ...]:
    """Read only the sparse DAgger authority stream from a committed MCAP."""

    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    mcap_path = episode_dir.resolve() / "episode.mcap"
    if not mcap_path.is_file():
        raise FileNotFoundError(mcap_path)
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(mcap_path), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topics = {item.name: item.type for item in reader.get_all_topics_and_types()}
    if AUTHORITY_TOPIC not in topics:
        return ()
    if topics[AUTHORITY_TOPIC] != AUTHORITY_TYPE:
        raise ValueError(
            f"unexpected {AUTHORITY_TOPIC} type: {topics[AUTHORITY_TOPIC]}"
        )
    message_type = get_message(AUTHORITY_TYPE)
    events = []
    while reader.has_next():
        topic, payload, bag_timestamp_ns = reader.read_next()
        if topic != AUTHORITY_TOPIC:
            continue
        message = deserialize_message(payload, message_type)
        events.append(
            AuthorityEventRecord(
                sequence=int(message.sequence),
                monotonic_time_ns=int(message.monotonic_time_ns),
                intervention_id=int(message.intervention_id),
                control_epoch=int(message.control_epoch),
                event_type=AuthorityEventType(int(message.event_type)),
                reason=str(message.reason),
                bag_timestamp_ns=int(bag_timestamp_ns),
                header_stamp_ns=(
                    int(message.header.stamp.sec) * 1_000_000_000
                    + int(message.header.stamp.nanosec)
                ),
            )
        )
    return tuple(events)
