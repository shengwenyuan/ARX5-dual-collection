from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from arx5_collection.collection_metadata import DaggerMetadata, MetadataContext
from jsonschema import Draft202012Validator, FormatChecker

from arx5_collection.episode.models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeState,
    RecordingStarted,
    RecordingStopping,
    StreamMetrics,
    StreamSpec,
)
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.ports import TriggerEvent, TriggerSignal
from arx5_collection.episode.store import EpisodeStore

from .fakes import FakeBackend, FakeMonitor, FakeTrigger


ROOT = Path(__file__).parents[2]
STATION_PATH = ROOT / "config" / "station.example.json"
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
        started: list[RecordingStarted] = []
        stopping: list[RecordingStopping] = []
        trigger = FakeTrigger(
                [True, False, True],
                clock_ns=iter_clock_ns([90.0, 190.25]),
            )
        runtime = self.runtime(
            trigger=trigger,
            wall_clock=lambda: next(wall_times),
            monotonic_clock=lambda: 100.0,
            recording_started_hook=started.append,
            recording_stopping_hook=stopping.append,
        )

        result = runtime.run_once(self.request())

        self.assertEqual(result.outcome, EpisodeOutcome.SUCCESS)
        self.assertEqual(result.duration_s, 90.25)
        self.assertEqual(started[0].monotonic_time_ns, 100_000_000_000)
        self.assertEqual(stopping[0].monotonic_time_ns, 190_250_000_000)
        self.assertTrue(result.committed)
        self.assertEqual(runtime.state, EpisodeState.READY)
        self.assertEqual(
            trigger.lifecycle,
            ["arm", "disarm", "arm", "disarm"],
        )
        self.assertEqual(
            {path.name for path in result.mcap_path.parent.iterdir()},
            {"episode.mcap", "metadata.json"},
        )
        metadata = json.loads(result.metadata_path.read_text())
        schema = json.loads(SCHEMA_PATH.read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(metadata)

    def test_required_failure_commits_failed_episode(self) -> None:
        runtime = self.runtime(
            trigger=FakeTrigger([True]),
            monitor=FakeMonitor(METRICS, failure="left_arm stopped"),
        )
        result = runtime.run_once(self.request())
        self.assertEqual(result.outcome, EpisodeOutcome.FAIL)
        self.assertEqual(result.errors, ("left_arm stopped",))
        self.assertTrue(result.session_blocked)
        self.assertTrue(result.committed)
        self.assertEqual(result.mcap_path.parent.parent, self.output_root / "fail")
        self.assertEqual(json.loads(result.metadata_path.read_text())["outcome"], "fail")

    def test_runtime_check_failure_commits_failed_episode(self) -> None:
        runtime = self.runtime(
            trigger=FakeTrigger([True]),
            runtime_check=lambda: (_ for _ in ()).throw(
                RuntimeError("d405-source exited")
            ),
        )

        result = runtime.run_once(self.request())

        self.assertEqual(result.outcome, EpisodeOutcome.FAIL)
        self.assertEqual(result.errors, ("d405-source exited",))
        self.assertTrue(result.session_blocked)

    def test_episode_scoped_failure_commits_without_blocking_session(self) -> None:
        runtime = self.runtime(
            trigger=FakeTrigger(
                [
                    True,
                    TriggerSignal(
                        TriggerEvent.FAIL,
                        20_000_000_000,
                        "DAgger policy rejected action",
                    ),
                ]
            ),
            monotonic_clock=lambda: 10.0,
        )

        result = runtime.run_once(self.request())

        self.assertEqual(result.outcome, EpisodeOutcome.FAIL)
        self.assertEqual(result.errors, ("DAgger policy rejected action",))
        self.assertFalse(result.session_blocked)
        self.assertEqual(result.duration_s, 10.0)

    def test_pre_episode_check_runs_after_start_before_recorder(self) -> None:
        events: list[str] = []

        class OrderedBackend(FakeBackend):
            def start(self, mcap_path, streams) -> None:
                events.append("recorder_started")
                super().start(mcap_path, streams)

        runtime = self.runtime(
            trigger=FakeTrigger([True, True]),
            backend=OrderedBackend(),
        )
        runtime.pre_episode_check = lambda: events.append("checked")
        result = runtime.run_once(self.request())
        self.assertTrue(result.committed)
        self.assertEqual(events, ["checked", "recorder_started"])

    def test_pre_episode_check_failure_does_not_create_partial(self) -> None:
        runtime = self.runtime(trigger=FakeTrigger([True]))
        runtime.pre_episode_check = lambda: (_ for _ in ()).throw(
            RuntimeError("not ready")
        )
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            runtime.run_once(self.request())
        self.assertFalse(self.output_root.exists())

    def test_recording_interrupt_commits_aborted_episode(self) -> None:
        runtime = self.runtime(trigger=FakeTrigger([True, KeyboardInterrupt()]))
        result = runtime.run_once(self.request())
        self.assertEqual(result.outcome, EpisodeOutcome.ABORTED)
        self.assertEqual(result.errors, ("recording interrupted",))
        self.assertEqual(result.mcap_path.parent.parent.name, "abort")

    def test_operator_abort_commits_under_abort(self) -> None:
        runtime = self.runtime(
            trigger=FakeTrigger([True, TriggerEvent.ABORT]),
        )
        result = runtime.run_once(self.request())
        self.assertEqual(result.outcome, EpisodeOutcome.ABORTED)
        self.assertEqual(result.errors, ("operator requested abort",))
        self.assertEqual(result.mcap_path.parent.parent, self.output_root / "abort")

    def test_stream_warning_does_not_change_success(self) -> None:
        metrics = (StreamMetrics(STREAM.id, 4_500, 90.0, 50.0, 40.0, ("low rate",)),)
        runtime = self.runtime(
            trigger=FakeTrigger([True, True]),
            monitor=FakeMonitor(metrics),
        )
        result = runtime.run_once(self.request())
        metadata = json.loads(result.metadata_path.read_text())
        self.assertEqual(result.outcome, EpisodeOutcome.SUCCESS)
        self.assertEqual(metadata["streams"][0]["warnings"], ["low rate"])

    def test_runtime_accepts_typed_dagger_metadata_context(self) -> None:
        runtime = self.runtime(
            trigger=FakeTrigger([True, True]),
            metadata_context_provider=lambda: MetadataContext.for_dagger(
                DaggerMetadata("a" * 64, 0, ())
            ),
        )
        result = runtime.run_once(self.request())
        metadata = json.loads(result.metadata_path.read_text())
        self.assertEqual(metadata["collection_type"], "dagger")
        self.assertEqual(metadata["dagger"]["checkpoint_sha256"], "a" * 64)

    def test_finalizer_runs_before_metadata_and_merges_extensions(self) -> None:
        calls: list[Path] = []

        class Finalizer:
            def finalize(self, mcap_path, streams, expected_metrics):
                self.assertions(mcap_path, streams, expected_metrics)
                return {
                    "mcap_compression": {
                        "algorithm": "zstd",
                        "status": "compressed",
                    }
                }

            def assertions(self, mcap_path, streams, expected_metrics):
                self_path = Path(mcap_path)
                assert self_path.read_bytes() == b"mcap"
                assert streams == (STREAM,)
                assert expected_metrics == METRICS
                calls.append(self_path)

        runtime = self.runtime(
            trigger=FakeTrigger([True, True]),
            finalizer=Finalizer(),
            metadata_extensions={"capture": {"profile": "rgb_only"}},
        )

        result = runtime.run_once(self.request())

        metadata = json.loads(result.metadata_path.read_text())
        self.assertEqual(len(calls), 1)
        self.assertEqual(metadata["extensions"]["capture"]["profile"], "rgb_only")
        self.assertEqual(
            metadata["extensions"]["mcap_compression"]["algorithm"],
            "zstd",
        )

    def test_finalization_failure_preserves_partial_and_stays_finalizing(self) -> None:
        trigger = FakeTrigger([True, True])
        runtime = self.runtime(
            trigger=trigger,
            backend=FakeBackend(stop_error=RuntimeError("close failed")),
        )
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            runtime.run_once(self.request())
        self.assertEqual(runtime.state, EpisodeState.FINALIZING)
        self.assertEqual(trigger.lifecycle[-1], "disarm")
        self.assertEqual(len(EpisodeStore(self.output_root).list_partials()), 1)

    def test_recording_started_hook_failure_still_closes_components(self) -> None:
        backend = FakeBackend()
        monitor = FakeMonitor(METRICS)
        runtime = self.runtime(
            trigger=FakeTrigger([True]),
            backend=backend,
            monitor=monitor,
            recording_started_hook=lambda started: (_ for _ in ()).throw(
                RuntimeError("model start failed")
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "model start failed"):
            runtime.run_once(self.request())
        self.assertEqual(backend.stop_count, 1)
        self.assertEqual(monitor.stop_count, 1)

    def test_recording_stopping_hook_runs_before_backend_stop(self) -> None:
        events: list[str] = []

        class OrderedBackend(FakeBackend):
            def stop(self) -> None:
                events.append("backend_stop")
                super().stop()

        runtime = self.runtime(
            trigger=FakeTrigger([True, True]),
            backend=OrderedBackend(),
            recording_stopping_hook=lambda stopping: events.append(
                f"control_stop:{stopping.outcome.value}"
            ),
        )
        runtime.run_once(self.request())
        self.assertEqual(events, ["control_stop:success", "backend_stop"])

    def test_commit_failure_preserves_complete_partial(self) -> None:
        store = EpisodeStore(self.output_root)
        runtime = self.runtime(trigger=FakeTrigger([True, True]), store=store)
        with patch.object(store, "commit", side_effect=OSError("rename failed")):
            with self.assertRaisesRegex(OSError, "rename failed"):
                runtime.run_once(self.request())

        partials = store.list_partials()
        self.assertEqual(runtime.state, EpisodeState.FINALIZING)
        self.assertEqual(len(partials), 1)
        self.assertEqual(
            {path.name for path in partials[0].iterdir()},
            {"episode.mcap", "metadata.json"},
        )

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
        store: EpisodeStore | None = None,
        backend: FakeBackend | None = None,
        monitor: FakeMonitor | None = None,
        episode_id_factory=None,
        wall_clock=None,
        monotonic_clock=None,
        metadata_context_provider=None,
        runtime_check=None,
        recording_started_hook=None,
        recording_stopping_hook=None,
        finalizer=None,
        metadata_extensions=None,
    ) -> EpisodeRuntime:
        return EpisodeRuntime(
            store=store or EpisodeStore(self.output_root),
            trigger=trigger,
            backend=backend or FakeBackend(),
            monitor=monitor or FakeMonitor(METRICS),
            software_version="0.1.0",
            episode_id_factory=episode_id_factory or (lambda: "episode-001"),
            wall_clock=wall_clock or (lambda: datetime.now(timezone.utc)),
            monotonic_clock=monotonic_clock or iter_clock([10.0, 100.0]),
            metadata_context_provider=metadata_context_provider,
            runtime_check=runtime_check,
            recording_started_hook=recording_started_hook,
            recording_stopping_hook=recording_stopping_hook,
            finalizer=finalizer,
            metadata_extensions=metadata_extensions,
            poll_interval_s=0.01,
        )


def iter_clock(values: list[float]):
    iterator = iter(values)
    return lambda: next(iterator)


def iter_clock_ns(values: list[float]):
    iterator = iter(values)
    return lambda: round(next(iterator) * 1e9)


if __name__ == "__main__":
    unittest.main()
