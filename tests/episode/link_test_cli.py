from __future__ import annotations

import io
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from arx5_collection.episode import cli as cli_module
from arx5_collection.episode.models import StreamMetrics
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.store import EpisodeStore

from fakes import FakeBackend, FakeMonitor, FakeTrigger


class ContextTrigger(FakeTrigger):
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        pass


def main() -> None:
    station_path = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        task_config = root / "task.json"
        output_root = root / "episodes"
        task_config.write_text(
            json.dumps(
                {
                    "task_id": "link-test",
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
        trigger = ContextTrigger([True, True, True, True])
        ids = iter(["link-001", "link-002"])
        clock_values = iter([0.0, 1.0, 2.0, 3.0])

        def runtime_factory(request, record_trigger):
            stream = request.streams[0]
            return EpisodeRuntime(
                store=EpisodeStore(request.output_root),
                trigger=record_trigger,
                backend=FakeBackend(),
                monitor=FakeMonitor(
                    (StreamMetrics(stream.id, 60, 1.0, 60.0, 18.0),)
                ),
                software_version="0.1.0",
                episode_id_factory=lambda: next(ids),
                wall_clock=lambda: datetime.now(timezone.utc),
                monotonic_clock=lambda: next(clock_values),
            )

        output = io.StringIO()
        exit_code = cli_module.run_cli(
            runtime_factory,
            [
                "--task-config",
                str(task_config),
                "--station-config",
                str(station_path),
                "--output-root",
                str(output_root),
                "--task-description",
                "Run installed CLI core",
                "--episodes",
                "2",
            ],
            trigger_factory=lambda key: trigger,
            stdout=output,
            stderr=io.StringIO(),
        )
        assert exit_code == 0
        assert len(output.getvalue().splitlines()) == 2
        assert len(list(output_root.iterdir())) == 2

    print(f"installed_from={cli_module.__file__}")
    print("episode_cli_core_link=ok")


if __name__ == "__main__":
    main()
