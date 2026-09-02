from __future__ import annotations

from arx5_collection.collection.episode.ports import RecordingBackend, StreamMonitor
from arx5_collection.collection.runtime.ports import (
    SessionArmController,
    SessionStreamMonitor,
)
from arx5_collection.adapters.ros2 import (
    RosDualArmResetController,
    RosStreamMonitor,
    RosbagRecordingBackend,
)


def test_ros_adapters_conform_to_frozen_episode_ports() -> None:
    backend = RosbagRecordingBackend(
        recorder_factory=lambda output_uri, topics, node_name: object()
    )
    monitor = RosStreamMonitor(backend, status_sink=None)
    assert isinstance(backend, RecordingBackend)
    assert isinstance(monitor, StreamMonitor)
    assert isinstance(monitor, SessionStreamMonitor)
    assert isinstance(RosDualArmResetController(), SessionArmController)
