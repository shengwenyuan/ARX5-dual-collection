from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import EpisodeRequest, EpisodeResult


def load_station(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text())
    devices = []

    for arm in config["arms"]:
        devices.append(
            {
                "id": f"{arm['name']}_arm",
                "kind": "arm",
                "serial_number": arm.get("usb_serial"),
                "configuration": {
                    "can_interface": arm["can_interface"],
                    "sdk_type": config["sdk_type"],
                },
            }
        )

    for name, camera in config["cameras"].items():
        camera_config = camera or {}
        devices.append(
            {
                "id": f"camera_{name}",
                "kind": "camera",
                "serial_number": camera_config.get("serial_number"),
                "configuration": {
                    key: value
                    for key, value in camera_config.items()
                    if key != "serial_number"
                },
            }
        )

    return {
        "id": config.get("station_id"),
        "config_schema_version": config["schema_version"],
        "devices": devices,
    }


def build_metadata(
    request: EpisodeRequest,
    result: EpisodeResult,
    station: dict[str, Any],
    software_version: str,
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
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

    return {
        "schema_version": 1,
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


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")


def format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("metadata timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
