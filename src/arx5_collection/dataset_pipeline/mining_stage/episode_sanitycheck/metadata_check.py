from __future__ import annotations

from typing import Any

from arx5_collection.dataset_pipeline.source.reader import load_metadata

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    def operation() -> dict[str, Any]:
        metadata = load_metadata(context.receipt.stage_dir)
        episode_id = _metadata_string(metadata, "episode_id")
        if episode_id != context.receipt.episode_id:
            raise ValueError("staged metadata episode_id does not match StageReceipt")
        collection_type = metadata.get("collection_type", "demonstration")
        if collection_type not in {"demonstration", "dagger"}:
            raise ValueError(f"unsupported collection_type: {collection_type!r}")
        outcome = _metadata_string(metadata, "outcome")
        _metadata_station_id(metadata)
        if (
            collection_type == "dagger"
            and outcome == "fail"
            and "dagger_fail" not in context.receipt.source_dir.parts
        ):
            raise ValueError("DAgger fail Episode must originate under dagger_fail/")
        return metadata

    context.metadata = timed(unit.type, operation)


def _metadata_string(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"staged metadata {key} must be a non-empty string")
    return value


def _metadata_station_id(metadata: dict[str, Any]) -> str:
    station = metadata.get("station")
    if not isinstance(station, dict):
        raise ValueError("staged metadata station must be an object")
    value = station.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("staged metadata station.id must be a non-empty string")
    return value
