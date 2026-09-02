from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from arx5_collection.collection.episode import runtime as runtime_module
from arx5_collection.collection.episode.models import (
    EpisodeRequest,
    StreamMetrics,
    StreamSpec,
)
from arx5_collection.collection.episode.store import EpisodeStore

from fakes import FakeBackend, FakeMonitor, FakeTrigger


def main() -> None:
    station_path = Path(sys.argv[1])
    stream = StreamSpec("left_arm", "/embodiments/left_arm/state", True, 60.0)
    metrics = (StreamMetrics(stream.id, 60, 1.0, 60.0, 18.0),)

    with tempfile.TemporaryDirectory() as directory:
        output_root = Path(directory) / "episodes"
        ids = iter(f"link-{index:02d}" for index in range(10))
        clock_values = iter(float(index) for index in range(20))
        runtime = runtime_module.EpisodeRuntime(
            store=EpisodeStore(output_root),
            trigger=FakeTrigger([True, True] * 10),
            backend=FakeBackend(),
            monitor=FakeMonitor(metrics),
            software_version="0.1.0",
            episode_id_factory=lambda: next(ids),
            wall_clock=lambda: datetime.now(timezone.utc),
            monotonic_clock=lambda: next(clock_values),
        )
        request = EpisodeRequest(
            task_id="link-test",
            task_description="Run ten installed Runtime episodes",
            output_root=output_root,
            station_config=station_path,
            streams=(stream,),
        )
        results = [runtime.run_once(request) for _ in range(10)]

        assert all(result.committed for result in results)
        assert len(list(output_root.iterdir())) == 10
        assert all(
            {path.name for path in result.mcap_path.parent.iterdir()}
            == {"episode.mcap", "metadata.json"}
            for result in results
        )

    print(f"installed_from={runtime_module.__file__}")
    print("episode_runtime_ten_episode_link=ok")


if __name__ == "__main__":
    main()
