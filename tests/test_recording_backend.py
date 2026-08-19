from __future__ import annotations

import pytest

from arx5_collection.episode.models import StreamSpec
from arx5_collection.ros2_adapters.recording import RosbagRecordingBackend


STREAMS = (
    StreamSpec("left_arm_state", "/embodiments/left_arm/state", True, 1000.0),
)


class FakeRecorder:
    def __init__(self, output_uri, topics, extra_file: bool = False) -> None:
        self.output_uri = output_uri
        self.topics = topics
        self.extra_file = extra_file
        self.calls = []

    def start_spin(self) -> None:
        self.calls.append("start_spin")

    def record(self) -> None:
        self.calls.append("record")
        self.output_uri.mkdir()
        (self.output_uri / "metadata.yaml").write_text("rosbag2_bagfile_information: {}")
        (self.output_uri / "episode_0.mcap").write_bytes(b"mcap")
        if self.extra_file:
            (self.output_uri / "unexpected.txt").write_text("unexpected")

    def stop(self) -> None:
        self.calls.append("stop")

    def stop_spin(self) -> None:
        self.calls.append("stop_spin")


def test_backend_normalizes_rosbag_directory_to_single_mcap(tmp_path) -> None:
    created = []

    def factory(output_uri, topics, node_name):
        recorder = FakeRecorder(output_uri, topics)
        created.append(recorder)
        return recorder

    backend = RosbagRecordingBackend(recorder_factory=factory)
    target = tmp_path / "episode.mcap"

    backend.start(target, STREAMS)
    backend.stop()

    assert target.read_bytes() == b"mcap"
    assert not (tmp_path / ".episode.mcap.rosbag2").exists()
    assert created[0].topics == ("/embodiments/left_arm/state",)
    assert created[0].calls == ["start_spin", "record", "stop", "stop_spin"]


def test_backend_supports_ten_consecutive_recordings(tmp_path) -> None:
    backend = RosbagRecordingBackend(
        recorder_factory=lambda output_uri, topics, node_name: FakeRecorder(
            output_uri, topics
        )
    )
    for index in range(10):
        target = tmp_path / f"episode-{index}.mcap"
        backend.start(target, STREAMS)
        backend.stop()
        assert target.is_file()


def test_backend_preserves_unexpected_rosbag_output(tmp_path) -> None:
    backend = RosbagRecordingBackend(
        recorder_factory=lambda output_uri, topics, node_name: FakeRecorder(
            output_uri,
            topics,
            extra_file=True,
        )
    )
    target = tmp_path / "episode.mcap"
    backend.start(target, STREAMS)

    with pytest.raises(RuntimeError, match="exactly one MCAP"):
        backend.stop()

    temporary = tmp_path / ".episode.mcap.rosbag2"
    assert temporary.is_dir()
    assert (temporary / "unexpected.txt").is_file()
    assert not target.exists()


def test_backend_reports_recorder_thread_failure(tmp_path) -> None:
    class BrokenRecorder:
        def start_spin(self) -> None:
            pass

        def record(self) -> None:
            raise RuntimeError("record failed")

        def stop(self) -> None:
            pass

        def stop_spin(self) -> None:
            pass

    backend = RosbagRecordingBackend(
        recorder_factory=lambda output_uri, topics, node_name: BrokenRecorder(),
        start_timeout_s=0.5,
    )

    with pytest.raises(RuntimeError, match="failed during start"):
        backend.start(tmp_path / "episode.mcap", STREAMS)


def test_backend_waits_for_subscription_readiness(tmp_path) -> None:
    calls = []

    def readiness(topics, node_name, timeout_s):
        calls.append((topics, node_name, timeout_s))

    backend = RosbagRecordingBackend(
        recorder_factory=lambda output_uri, topics, node_name: FakeRecorder(
            output_uri, topics
        ),
        readiness_probe=readiness,
    )
    target = tmp_path / "episode.mcap"

    backend.start(target, STREAMS)
    backend.stop()

    assert calls[0][0] == ("/embodiments/left_arm/state",)
    assert calls[0][1].startswith("episode_recorder_")
    assert 0 < calls[0][2] <= 5.0


def test_backend_records_additional_topics_without_monitoring_them(tmp_path) -> None:
    created = []

    def factory(output_uri, topics, node_name):
        recorder = FakeRecorder(output_uri, topics)
        created.append(recorder)
        return recorder

    backend = RosbagRecordingBackend(
        recorder_factory=factory,
        additional_topics=("/dagger/authority",),
    )
    target = tmp_path / "episode.mcap"

    backend.start(target, STREAMS)
    backend.stop()

    assert created[0].topics == (
        "/embodiments/left_arm/state",
        "/dagger/authority",
    )
    assert target.is_file()


def test_backend_rejects_additional_topic_that_duplicates_a_stream(tmp_path) -> None:
    backend = RosbagRecordingBackend(
        recorder_factory=lambda output_uri, topics, node_name: FakeRecorder(
            output_uri, topics
        ),
        additional_topics=("/embodiments/left_arm/state",),
    )

    with pytest.raises(ValueError, match="duplicate monitored streams"):
        backend.start(tmp_path / "episode.mcap", STREAMS)
