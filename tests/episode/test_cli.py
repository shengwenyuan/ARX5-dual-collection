from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from arx5_collection.episode.cli import load_request, run_cli, run_episode_loop
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.store import EpisodeStore

from .fakes import FakeBackend, FakeMonitor, FakeTrigger
from arx5_collection.episode.models import EpisodeBlocked, StreamMetrics
from arx5_collection.episode.ports import TriggerEvent, TriggerSignal


ROOT = Path(__file__).parents[2]
STATION_PATH = ROOT / "config" / "station.example.json"


class ContextTrigger(FakeTrigger):
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        self.closed = True


class EpisodeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.output_root = self.root / "episodes"
        self.task_config = self.root / "task.json"
        self.task_config.write_text(
            json.dumps(
                {
                    "task_id": "pick",
                    "task_description": "Pick the object",
                    "streams": [
                        {
                            "id": "left_arm",
                            "topic": "/embodiments/left_arm/state",
                            "required": True,
                            "expected_hz": 60.0,
                        }
                    ],
                }
            )
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_load_request_is_strict(self) -> None:
        request = load_request(
            self.task_config,
            self.output_root,
            STATION_PATH,
            task_description=" Folding 衣服 ",
        )
        self.assertEqual(request.streams[0].expected_hz, 60.0)
        self.assertEqual(request.task_description, " Folding 衣服 ")

        invalid = json.loads(self.task_config.read_text())
        invalid["frame_count"] = 1
        self.task_config.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            load_request(self.task_config, self.output_root, STATION_PATH)

    def test_run_cli_records_two_episodes_and_reports_partial(self) -> None:
        EpisodeStore(self.output_root).prepare("stale")
        trigger = ContextTrigger([True, True, True, True])
        output = io.StringIO()
        errors = io.StringIO()

        exit_code = run_cli(
            runtime_factory=self.runtime_factory(),
            argv=self.argv(2),
            trigger_factory=lambda key: trigger,
            stdout=output,
            stderr=errors,
        )

        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["outcome"] == "success" for row in rows))
        self.assertIn(".stale.partial", errors.getvalue())
        self.assertTrue(trigger.closed)

    def test_idle_keyboard_interrupt_exits_cleanly(self) -> None:
        trigger = ContextTrigger([KeyboardInterrupt()])
        exit_code = run_cli(
            runtime_factory=self.runtime_factory(),
            argv=self.argv(0),
            trigger_factory=lambda key: trigger,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(trigger.closed)

    def test_recording_interrupt_aborts_and_exits_cleanly(self) -> None:
        trigger = ContextTrigger([True, KeyboardInterrupt(), True, True])
        exit_code = run_cli(
            runtime_factory=self.runtime_factory(),
            argv=self.argv(0),
            trigger_factory=lambda key: trigger,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(list(self.output_root.iterdir())), 1)

    def test_a_aborts_current_episode_and_continues(self) -> None:
        trigger = ContextTrigger(
            [True, TriggerEvent.ABORT, True, True]
        )
        output = io.StringIO()
        exit_code = run_cli(
            runtime_factory=self.runtime_factory(),
            argv=self.argv(2),
            trigger_factory=lambda key: trigger,
            stdout=output,
            stderr=io.StringIO(),
        )
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual([row["outcome"] for row in rows], ["aborted", "success"])
        self.assertIn("/abort/", rows[0]["mcap_path"])

    def test_failed_episode_keeps_the_session_ready(self) -> None:
        trigger = ContextTrigger([True, True, True])
        request = load_request(self.task_config, self.output_root, STATION_PATH)
        runtime = self.runtime_factory(fail_first_episode=True)(request, trigger)
        output = io.StringIO()
        errors = io.StringIO()

        exit_code = run_episode_loop(
            runtime,
            request,
            episodes=2,
            stdout=output,
            stderr=errors,
        )

        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual([row["outcome"] for row in rows], ["fail", "success"])
        self.assertEqual(errors.getvalue().count("SESSION BLOCKED"), 1)
        self.assertEqual(errors.getvalue().count("\a"), 1)
        self.assertIn("EPISODE FAILED - SESSION BLOCKED", errors.getvalue())
        self.assertIn("result: fail", errors.getvalue())

    def test_episode_scoped_failure_returns_ready_without_block_banner(self) -> None:
        trigger = ContextTrigger(
            [
                True,
                TriggerSignal(TriggerEvent.FAIL, 1, "policy rejected action"),
                True,
                True,
            ]
        )
        request = load_request(self.task_config, self.output_root, STATION_PATH)
        runtime = self.runtime_factory()(request, trigger)
        output = io.StringIO()
        errors = io.StringIO()

        exit_code = run_episode_loop(
            runtime,
            request,
            episodes=2,
            stdout=output,
            stderr=errors,
        )

        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual([row["outcome"] for row in rows], ["fail", "success"])
        self.assertNotIn("SESSION BLOCKED", errors.getvalue())
        self.assertIn("EPISODE FAILED - SESSION READY", errors.getvalue())

    def test_pre_episode_block_does_not_create_an_empty_episode(self) -> None:
        trigger = ContextTrigger([True, True, True])
        request = load_request(self.task_config, self.output_root, STATION_PATH)
        runtime = self.runtime_factory()(request, trigger)
        attempts = 0

        def check() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise EpisodeBlocked(
                    "camera not ready",
                    "dual-arm G_COMPENSATION confirmed",
                )

        runtime.pre_episode_check = check
        output = io.StringIO()
        errors = io.StringIO()

        exit_code = run_episode_loop(
            runtime,
            request,
            episodes=1,
            stdout=output,
            stderr=errors,
        )

        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual([row["outcome"] for row in rows], ["success"])
        self.assertEqual(attempts, 2)
        self.assertEqual(errors.getvalue().count("SESSION BLOCKED"), 1)
        self.assertEqual(errors.getvalue().count("\a"), 1)
        self.assertIn("result: not_started", errors.getvalue())
        self.assertFalse(any(self.output_root.glob(".*.partial")))

    def test_finalization_failure_never_announces_ready_again(self) -> None:
        trigger = ContextTrigger([True, True])
        request = load_request(self.task_config, self.output_root, STATION_PATH)
        runtime = self.runtime_factory()(request, trigger)

        class BrokenFinalizer:
            def finalize(self, mcap_path, streams, expected_metrics):
                raise RuntimeError("compression failed")

        runtime.finalizer = BrokenFinalizer()
        errors = io.StringIO()

        with self.assertRaisesRegex(RuntimeError, "compression failed"):
            run_episode_loop(
                runtime,
                request,
                episodes=2,
                stdout=io.StringIO(),
                stderr=errors,
            )

        self.assertEqual(errors.getvalue().count("READY:"), 1)
        self.assertEqual(errors.getvalue().count("FINALIZING"), 1)
        self.assertEqual(runtime.state.value, "finalizing")

    def runtime_factory(self, fail_first_episode: bool = False):
        ids = iter(["episode-001", "episode-002"])
        clock_values = iter([0.0, 1.0, 2.0, 3.0])

        def factory(request, trigger):
            stream = request.streams[0]
            metrics = (StreamMetrics(stream.id, 60, 1.0, 60.0, 18.0),)
            monitor = FakeMonitor(metrics)
            if fail_first_episode:
                original_start = monitor.start
                starts = 0

                def start(streams) -> None:
                    nonlocal starts
                    starts += 1
                    original_start(streams)
                    monitor.failure = "left_arm data stopped" if starts == 1 else None

                monitor.start = start  # type: ignore[method-assign]
            return EpisodeRuntime(
                store=EpisodeStore(request.output_root),
                trigger=trigger,
                backend=FakeBackend(),
                monitor=monitor,
                software_version="0.1.0",
                episode_id_factory=lambda: next(ids),
                wall_clock=lambda: datetime.now(timezone.utc),
                monotonic_clock=lambda: next(clock_values),
            )

        return factory

    def argv(self, episodes: int) -> list[str]:
        return [
            "--task-config",
            str(self.task_config),
            "--station-config",
            str(STATION_PATH),
            "--output-root",
            str(self.output_root),
            "--episodes",
            str(episodes),
        ]


if __name__ == "__main__":
    unittest.main()
