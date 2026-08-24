from __future__ import annotations

from arx5_collection.episode.models import StreamSpec
from arx5_collection.ros2_adapters.health import StatusSample, StreamHealthTracker


STREAMS = (
    StreamSpec("camera_left_color", "/camera/left", True, 30.0),
    StreamSpec("optional", "/optional", False, 10.0),
)


def sample(
    stream_id: str = "camera_left_color",
    topic: str = "/camera/left",
    total_count: int = 100,
    observed_hz: float = 30.0,
    silence_s: float = 0.0,
) -> StatusSample:
    return StatusSample(
        stream_id=stream_id,
        topic=topic,
        total_count=total_count,
        window_count=30,
        observed_hz=observed_hz,
        max_gap_ms=34.0,
        silence_s=silence_s,
        non_monotonic_count=0,
    )


def test_missing_telemetry_warns_after_startup_grace() -> None:
    tracker = StreamHealthTracker(STREAMS, started_s=10.0)
    assert tracker.required_failure(12.9) is None
    assert tracker.required_failure(13.0) is None
    assert any(
        "produced no telemetry" in warning
        for warning in tracker.warnings_for("camera_left_color")
    )


def test_heartbeat_warns_but_data_silence_is_a_required_failure() -> None:
    heartbeat = StreamHealthTracker(STREAMS, started_s=0.0)
    heartbeat.observe(sample(), arrival_s=1.0)
    assert heartbeat.required_failure(3.4) is None
    assert heartbeat.required_failure(3.5) is None
    assert any(
        "telemetry stopped" in warning
        for warning in heartbeat.warnings_for("camera_left_color")
    )

    silence = StreamHealthTracker(STREAMS, started_s=0.0)
    silence.observe(sample(silence_s=2.0), arrival_s=1.0)
    assert "data stopped" in silence.required_failure(1.0)


def test_low_frequency_warns_but_does_not_fail() -> None:
    tracker = StreamHealthTracker(STREAMS, started_s=0.0)
    tracker.observe(sample(observed_hz=20.0), arrival_s=1.0)
    assert tracker.required_failure(1.0) is None
    assert "below" in tracker.warnings_for("camera_left_color")[0]


def test_optional_stream_does_not_trigger_failure() -> None:
    tracker = StreamHealthTracker((STREAMS[1],), started_s=0.0)
    assert tracker.required_failure(100.0) is None
    assert "produced no telemetry" in tracker.warnings_for("optional")[0]


def test_display_uses_episode_relative_count() -> None:
    tracker = StreamHealthTracker(STREAMS, started_s=0.0)
    tracker.observe(sample(total_count=100), arrival_s=1.0)
    tracker.observe(sample(total_count=130), arrival_s=2.0)
    assert "camera_left_color=30 30.0Hz" in tracker.display()
