from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from arx5_collection.collection.episode import metadata as metadata_module
from arx5_collection.collection.episode.models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeResult,
    StreamMetrics,
    StreamSpec,
)


def main() -> None:
    station_path = Path(sys.argv[1])
    schema_path = Path(sys.argv[2])
    stream = StreamSpec("left_arm", "/embodiments/left_arm/state", True, 60.0)
    request = EpisodeRequest(
        task_id="link-test",
        task_description="Validate installed metadata writer",
        output_root=Path("episodes"),
        station_config=station_path,
        streams=(stream,),
    )
    now = datetime.now(timezone.utc)
    result = EpisodeResult(
        episode_id="link-episode",
        outcome=EpisodeOutcome.SUCCESS,
        started_at=now,
        ended_at=now,
        duration_s=1.0,
        committed=False,
        mcap_path=Path("episode.mcap"),
        metadata_path=Path("metadata.json"),
        stream_metrics=(StreamMetrics(stream.id, 60, 1.0, 60.0, 18.0),),
    )

    metadata = metadata_module.build_metadata(
        request,
        result,
        metadata_module.load_station(station_path),
        "0.1.0",
    )
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "metadata.json"
        metadata_module.write_metadata(output, metadata)
        schema = json.loads(schema_path.read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(
            json.loads(output.read_text())
        )

    print(f"installed_from={metadata_module.__file__}")
    print("episode_metadata_writer_link=ok")


if __name__ == "__main__":
    main()
