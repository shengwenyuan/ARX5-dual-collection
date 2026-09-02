from __future__ import annotations

from pathlib import Path

from arx5_collection.dataset_pipeline.persistence.artifacts import read_json

from arx5_collection.dataset_pipeline.execution.models import ConversionStatus
from arx5_collection.dataset_pipeline.execution.models import EpisodeConversionResult


FRAGMENT_SCHEMA_VERSION = 2


def load_committed_fragment(fragment_dir: Path) -> EpisodeConversionResult:
    committed = read_json(fragment_dir / "COMMITTED.json")
    fragment = read_json(fragment_dir / "fragment.json")
    if committed.get("schema_version") != FRAGMENT_SCHEMA_VERSION:
        raise ValueError("unsupported committed Fragment schema_version")
    if fragment.get("schema_version") != FRAGMENT_SCHEMA_VERSION:
        raise ValueError("unsupported Fragment schema_version")
    episode_id = committed.get("episode_id")
    if (
        not isinstance(episode_id, str)
        or fragment.get("episode_id") != episode_id
        or committed.get("fragment_status") != ConversionStatus.COMMITTED.value
        or fragment.get("status") != ConversionStatus.COMMITTED.value
    ):
        raise ValueError("committed Fragment identity or status mismatch")
    return EpisodeConversionResult(
        episode_id=episode_id,
        status=ConversionStatus.COMMITTED,
        fragment_dir=fragment_dir,
        segment_count=_non_negative_int(
            fragment.get("segment_count"),
            "segment_count",
        ),
        frame_count=_non_negative_int(fragment.get("frame_count"), "frame_count"),
    )


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Fragment {label} must be a non-negative integer")
    return value
