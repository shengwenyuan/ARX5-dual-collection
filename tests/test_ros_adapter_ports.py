from __future__ import annotations

from arx5_collection.episode.ports import RecordingBackend, StreamMonitor
from arx5_collection.production.ports import SessionArmController, SessionStreamMonitor
from arx5_collection.ros2_adapters import (
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
