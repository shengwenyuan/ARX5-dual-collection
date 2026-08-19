from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arx5_collection.collection_metadata import CollectionType, MetadataContext
from arx5_collection.production.config import load_station_config

from .models import EpisodeRequest, EpisodeResult


def load_station(path: Path) -> dict[str, Any]:
    return load_station_config(path).metadata()


def build_metadata(
    request: EpisodeRequest,
    result: EpisodeResult,
    station: dict[str, Any],
    software_version: str,
    extensions: dict[str, Any] | None = None,
    metadata_context: MetadataContext | None = None,
) -> dict[str, Any]:
    context = metadata_context or MetadataContext.demonstration()
    metrics_by_id = {metrics.id: metrics for metrics in result.stream_metrics}
    stream_ids = {stream.id for stream in request.streams}
    if len(metrics_by_id) != len(result.stream_metrics) or set(metrics_by_id) != stream_ids:
        raise ValueError("StreamSpec and StreamMetrics ids must match")

    streams = []
    for stream in request.streams:
        metrics = metrics_by_id[stream.id]
        streams.append(
            {
                "id": stream.id,
                "topic": stream.topic,
                "required": stream.required,
                "expected_hz": stream.expected_hz,
                "message_count": metrics.count,
                "observed_hz": metrics.observed_hz,
                "max_gap_ms": metrics.max_gap_ms,
                "warnings": list(metrics.warnings),
            }
        )

    metadata = {
        "schema_version": 1,
        "collection_type": context.collection_type.value,
        "episode_id": result.episode_id,
        "task": {
            "id": request.task_id,
            "description": request.task_description,
        },
        "outcome": result.outcome.value,
        "timing": {
            "started_at": format_utc(result.started_at),
            "ended_at": format_utc(result.ended_at),
            "duration_s": result.duration_s,
        },
        "station": station,
        "streams": streams,
        "calibration": {"intrinsics": None, "extrinsics": None},
        "software": {
            "name": "arx5-dual-collection",
            "version": software_version,
        },
        "errors": list(result.errors),
        "extensions": extensions or {},
    }
    if context.collection_type is CollectionType.DAGGER:
        assert context.dagger is not None
        metadata["dagger"] = context.dagger.to_dict()
    return metadata


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")


def format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("metadata timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
