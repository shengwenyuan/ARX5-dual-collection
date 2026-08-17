from __future__ import annotations

from pathlib import Path
from typing import Any

from arx5_collection.artifacts import write_json
from arx5_collection.artifacts import write_jsonl
from arx5_collection.atomic import staged_directory


def write_cleaning_artifacts(
    output_root: Path,
    episode_id: str,
    quality: dict[str, Any],
    frame_groups: list[dict[str, Any]],
) -> Path:
    target = output_root / episode_id
    with staged_directory(target) as temporary:
        write_json(temporary / "quality.json", quality)
        write_jsonl(temporary / "frame_index.jsonl", frame_groups)
    return target
