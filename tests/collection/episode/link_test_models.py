from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from arx5_collection.collection.episode import models


def main() -> None:
    stream = models.StreamSpec(
        id="camera_front_color",
        topic="/sensors/camera_front/color",
        required=True,
        expected_hz=30.0,
    )
    request = models.EpisodeRequest(
        task_id="smoke-test",
        task_description="Verify the installed Episode model chain",
        output_root=Path("episodes"),
        station_config=Path("config/station.yaml"),
        streams=(stream,),
    )
    metrics = models.StreamMetrics(
        id=stream.id,
        count=2_700,
        duration_s=90.0,
        observed_hz=30.0,
        max_gap_ms=35.0,
    )
    now = datetime.now(timezone.utc)
    result = models.EpisodeResult(
        episode_id="smoke-episode",
        outcome=models.EpisodeOutcome.SUCCESS,
        started_at=now,
        ended_at=now,
        duration_s=90.0,
        committed=True,
        mcap_path=Path("episodes/smoke-episode/episode.mcap"),
        metadata_path=Path("episodes/smoke-episode/metadata.json"),
        stream_metrics=(metrics,),
    )

    assert request.streams[0].id == result.stream_metrics[0].id
    assert result.committed
    print(f"installed_from={models.__file__}")
    print("episode_models_link=ok")


if __name__ == "__main__":
    main()
