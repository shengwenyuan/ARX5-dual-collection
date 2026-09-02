from __future__ import annotations

import unittest
from pathlib import Path

from arx5_collection.collection.episode.models import StreamMetrics, StreamSpec
from arx5_collection.collection.episode.ports import (
    RecordTrigger,
    RecordingBackend,
    StreamMonitor,
)


STREAM = StreamSpec(
    id="camera_front_color",
    topic="/sensors/camera_front/color",
    required=True,
    expected_hz=30.0,
)


class FakeTrigger:
    def __init__(self, presses: list[bool]) -> None:
        self.presses = iter(presses)

    def arm(self) -> None:
        pass

    def disarm(self) -> None:
        pass

    def wait(self, timeout_s: float) -> bool:
        return next(self.presses, False)


class FakeBackend:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self, mcap_path: Path, streams: tuple[StreamSpec, ...]) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeMonitor:
    def __init__(self) -> None:
        self.failure: str | None = None

    def start(self, streams: tuple[StreamSpec, ...]) -> None:
        self.streams = streams

    def required_failure(self) -> str | None:
        return self.failure

    def stop(self) -> tuple[StreamMetrics, ...]:
        return (
            StreamMetrics(
                id=self.streams[0].id,
                count=2_700,
                duration_s=90.0,
                observed_hz=30.0,
                max_gap_ms=35.0,
            ),
        )


class EpisodePortsTest(unittest.TestCase):
    def test_protocols_accept_structural_implementations(self) -> None:
        self.assertIsInstance(FakeTrigger([]), RecordTrigger)
        self.assertIsInstance(FakeBackend(), RecordingBackend)
        self.assertIsInstance(FakeMonitor(), StreamMonitor)

    def test_fake_contract_chain(self) -> None:
        trigger = FakeTrigger([False, True])
        backend = FakeBackend()
        monitor = FakeMonitor()

        backend.start(Path("episode.mcap"), (STREAM,))
        monitor.start((STREAM,))
        while not trigger.wait(0.1):
            self.assertIsNone(monitor.required_failure())
        backend.stop()
        metrics = monitor.stop()

        self.assertTrue(backend.started)
        self.assertTrue(backend.stopped)
        self.assertEqual(metrics[0].id, STREAM.id)

    def test_required_failure_can_be_polled(self) -> None:
        monitor = FakeMonitor()
        monitor.start((STREAM,))
        monitor.failure = "camera_front_color stopped"
        self.assertEqual(
            monitor.required_failure(),
            "camera_front_color stopped",
        )


if __name__ == "__main__":
    unittest.main()
