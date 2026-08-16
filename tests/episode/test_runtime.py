from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from arx5_collection.episode.models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeState,
    StreamMetrics,
    StreamSpec,
)
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.store import EpisodeStore

from .fakes import FakeBackend, FakeMonitor, FakeTrigger


ROOT = Path(__file__).parents[2]
STATION_PATH = ROOT / "config" / "station.w3.json"
SCHEMA_PATH = ROOT / "schemas" / "episode-metadata-v1.json"
STREAM = StreamSpec("left_arm", "/embodiments/left_arm/state", True, 60.0)
METRICS = (StreamMetrics(STREAM.id, 5_400, 90.0, 60.0, 18.0),)


class EpisodeRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary_directory.name) / "episodes"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_success_uses_monotonic_duration_and_commits(self) -> None:
        wall_times = iter(
            [
                datetime(2026, 8, 16, 2, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 16, 1, 59, tzinfo=timezone.utc),
            ]
        )
        monotonic_times = iter([100.0, 190.25])
        runtime = self.runtime(
            trigger=FakeTrigger([True, False, True]),
            wall_clock=lambda: next(wall_times),
            monotonic_clock=lambda: next(monotonic_times),
        )

        result = runtime.run_once(self.request())

        self.assertEqual(result.outcome, EpisodeOutcome.SUCCESS)
        self.assertEqual(result.duration_s, 90.25)
        self.assertTrue(result.committed)
        self.assertEqual(runtime.state, EpisodeState.READY)
        self.assertEqual(
            {path.name for path in result.mcap_path.parent.iterdir()},
            {"episode.mcap", "metadata.json"},
        )
        metadata = json.loads(result.metadata_path.read_text())
        schema = json.loads(SCHEMA_PATH.read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(metadata)

    def test_required_failure_commits_aborted_episode(self) -> None:
        runtime = self.runtime(
            trigger=FakeTrigger([True]),
            monitor=FakeMonitor(METRICS, failure="left_arm stopped"),
        )
        result = runtime.run_once(self.request())
        self.assertEqual(result.outcome, EpisodeOutcome.ABORTED)
        self.assertEqual(result.errors, ("left_arm stopped",))
        self.assertTrue(result.committed)

    def test_recording_interrupt_commits_aborted_episode(self) -> None:
        runtime = self.runtime(trigger=FakeTrigger([True, KeyboardInterrupt()]))
        result = runtime.run_once(self.request())
        self.assertEqual(result.outcome, EpisodeOutcome.ABORTED)
        self.assertEqual(result.errors, ("recording interrupted",))

    def test_finalization_failure_preserves_partial_and_resets_state(self) -> None:
        runtime = self.runtime(
            trigger=FakeTrigger([True, True]),
            backend=FakeBackend(stop_error=RuntimeError("close failed")),
        )
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            runtime.run_once(self.request())
        self.assertEqual(runtime.state, EpisodeState.READY)
        self.assertEqual(len(EpisodeStore(self.output_root).list_partials()), 1)

    def test_ten_episodes_run_without_restarting_runtime(self) -> None:
        ids = iter(f"episode-{index:02d}" for index in range(10))
        times = iter(float(index) for index in range(20))
        runtime = self.runtime(
            trigger=FakeTrigger([True, True] * 10),
            episode_id_factory=lambda: next(ids),
            monotonic_clock=lambda: next(times),
        )
        results = [runtime.run_once(self.request()) for _ in range(10)]
        self.assertTrue(all(result.committed for result in results))
        self.assertEqual(len(list(self.output_root.iterdir())), 10)
        self.assertEqual(runtime.state, EpisodeState.READY)

    def request(self) -> EpisodeRequest:
        return EpisodeRequest(
            task_id="pick",
            task_description="Pick the object",
            output_root=self.output_root,
            station_config=STATION_PATH,
            streams=(STREAM,),
        )

    def runtime(
        self,
        trigger: FakeTrigger,
        backend: FakeBackend | None = None,
        monitor: FakeMonitor | None = None,
        episode_id_factory=None,
        wall_clock=None,
        monotonic_clock=None,
    ) -> EpisodeRuntime:
        return EpisodeRuntime(
            store=EpisodeStore(self.output_root),
            trigger=trigger,
            backend=backend or FakeBackend(),
            monitor=monitor or FakeMonitor(METRICS),
            software_version="0.1.0",
            episode_id_factory=episode_id_factory or (lambda: "episode-001"),
            wall_clock=wall_clock or (lambda: datetime.now(timezone.utc)),
            monotonic_clock=monotonic_clock or iter_clock([10.0, 100.0]),
            poll_interval_s=0.01,
        )


def iter_clock(values: list[float]):
    iterator = iter(values)
    return lambda: next(iterator)


if __name__ == "__main__":
    unittest.main()
