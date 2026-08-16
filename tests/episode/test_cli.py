from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from arx5_collection.episode.cli import load_request, run_cli
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.store import EpisodeStore

from .fakes import FakeBackend, FakeMonitor, FakeTrigger
from arx5_collection.episode.models import StreamMetrics


ROOT = Path(__file__).parents[2]
STATION_PATH = ROOT / "config" / "station.w3.json"


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
        request = load_request(self.task_config, self.output_root, STATION_PATH)
        self.assertEqual(request.streams[0].expected_hz, 60.0)

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

    def test_aborted_episode_exits_the_session(self) -> None:
        trigger = ContextTrigger([True, KeyboardInterrupt(), True, True])
        exit_code = run_cli(
            runtime_factory=self.runtime_factory(),
            argv=self.argv(0),
            trigger_factory=lambda key: trigger,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(len(list(self.output_root.iterdir())), 1)

    def runtime_factory(self):
        ids = iter(["episode-001", "episode-002"])
        clock_values = iter([0.0, 1.0, 2.0, 3.0])

        def factory(request, trigger):
            stream = request.streams[0]
            metrics = (StreamMetrics(stream.id, 60, 1.0, 60.0, 18.0),)
            return EpisodeRuntime(
                store=EpisodeStore(request.output_root),
                trigger=trigger,
                backend=FakeBackend(),
                monitor=FakeMonitor(metrics),
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
