from __future__ import annotations

from types import SimpleNamespace

from arx5_collection.collection.episode.models import StreamMetrics, StreamSpec
from arx5_collection.adapters.ros2.monitor import RosStreamMonitor


STREAM = StreamSpec("camera_left_color", "/camera/left", True, 30.0)


class FakeBackend:
    def metrics(self, streams):
        return (StreamMetrics(STREAM.id, 30, 1.0, 30.0, 34.0),)


class FakeContext:
    def __init__(self) -> None:
        self.shutdown_called = False

    def ok(self) -> bool:
        return not self.shutdown_called

    def shutdown(self) -> None:
        self.shutdown_called = True


def status(total_count: int):
    return SimpleNamespace(
        stream_id=STREAM.id,
        topic=STREAM.topic,
        total_count=total_count,
        window_count=30,
        observed_hz=30.0,
        max_gap_ms=34.0,
        silence_s=0.0,
        non_monotonic_count=0,
    )


def test_episode_cycles_reuse_session_subscription_and_reset_baseline() -> None:
    now = [0.0]
    output: list[str] = []
    context = FakeContext()
    monitor = RosStreamMonitor(
        FakeBackend(),  # type: ignore[arg-type]
        status_sink=output.append,
        display_period_s=0.1,
        clock=lambda: now[0],
    )
    monitor._context = context
    monitor._status_callback(status(100))
    monitor.wait_until_ready((STREAM.id,), 1.0, lambda: None)

    for episode in range(20):
        now[0] = float(episode + 1)
        monitor._status_callback(status(100 + episode * 30))
        monitor.start((STREAM,))
        now[0] += 0.2
        assert monitor.required_failure() is None
        assert "camera_left_color=0" in output[-1]
        monitor.stop()
        assert monitor._context is context

    monitor.close()
    assert context.shutdown_called


def test_stale_idle_snapshot_warns_without_failing_episode() -> None:
    now = [0.0]
    context = FakeContext()
    monitor = RosStreamMonitor(
        FakeBackend(),  # type: ignore[arg-type]
        status_sink=None,
        clock=lambda: now[0],
    )
    assert monitor.display_period_s == 2.0
    monitor._context = context
    monitor._status_callback(status(100))

    now[0] = 4.0
    monitor.start((STREAM,))
    now[0] = 7.0
    assert monitor.required_failure() is None
    metrics = monitor.stop()
    assert any("produced no telemetry" in warning for warning in metrics[0].warnings)
    monitor.close()
