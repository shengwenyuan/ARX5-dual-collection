from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import TypeVar

from arx5_collection.dataset_pipeline.persistence.artifacts import read_json
from arx5_collection.dataset_pipeline.persistence.artifacts import read_jsonl
from arx5_collection.dataset_pipeline.persistence.atomic import staged_directory

from arx5_collection.dataset_pipeline.configuration.recipe import DatasetPipelineRecipe
from arx5_collection.dataset_pipeline.persistence.fragment import (
    FRAGMENT_SCHEMA_VERSION,
)
from arx5_collection.dataset_pipeline.source.staging import validate_stage
from .models import ConversionStatus
from .models import EpisodeConversionResult
from .models import FileIdentity
from .models import StageReceipt
from .episode_pipeline import run_episode_pipeline


_REASON_COMPONENT = re.compile(r"^[a-z][a-z0-9_]*$")
T = TypeVar("T")


class _Excluded(RuntimeError):
    def __init__(self, result: EpisodeConversionResult) -> None:
        self.result = result


def convert_episode_fragment(
    receipt: StageReceipt,
    fragment_target: Path,
    recipe: DatasetPipelineRecipe,
    task: str,
    repo_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> EpisodeConversionResult:
    """Convert one staged source Episode into one committed v2.1 Fragment."""

    phases: list[tuple[str, float]] = []

    def timed(name: str, operation: Callable[[], T]) -> T:
        started = time.monotonic()
        try:
            return operation()
        finally:
            phases.append((name, max(time.monotonic() - started, 0.0)))

    if timed("stage_validate", lambda: validate_stage(receipt.stage_dir)) != receipt:
        raise ValueError("StageReceipt does not match committed staging")
    if not fragment_target.is_absolute():
        raise ValueError("Fragment target must be an absolute path")
    if not task.strip():
        raise ValueError("training task must not be empty")
    if "/" not in repo_id or repo_id.startswith("/") or repo_id.endswith("/"):
        raise ValueError("repo_id must use the '<owner>/<dataset>' form")

    episode_id = receipt.episode_id

    try:
        with staged_directory(fragment_target) as temporary:
            pipeline = run_episode_pipeline(
                receipt,
                temporary,
                recipe,
                task,
                repo_id,
            )
            phases.extend(pipeline.phase_seconds)
            if pipeline.exclusion_reason is not None:
                raise _Excluded(
                    _excluded_result(
                        receipt,
                        pipeline.selection.excluded_episodes,
                        tuple(phases),
                    )
                )
            if pipeline.selection.output_dir != temporary / "selection":
                raise RuntimeError("selector wrote outside the Fragment workspace")
            if pipeline.dataset_root != temporary / "lerobot":
                raise RuntimeError(
                    "Dataset generator wrote outside the Fragment workspace"
                )
            if pipeline.validation is None:
                raise RuntimeError("Dataset generator did not validate the Fragment")
            metadata = pipeline.metadata
            collection_type = str(metadata.get("collection_type", "demonstration"))
            outcome = _metadata_string(metadata, "outcome")
            station_id = _metadata_station_id(metadata)
            finalize_started = time.monotonic()
            report_path = temporary / "reports" / "conversion.json"
            conversion = read_json(report_path)
            expected_gripper = {
                "contract_id": recipe.gripper_contract,
                "left": asdict(recipe.gripper),
                "right": asdict(recipe.gripper),
            }
            if conversion.get("gripper_calibration") != expected_gripper:
                raise RuntimeError(
                    "conversion gripper calibration does not match device contract"
                )
            _rewrite_report_paths(conversion, fragment_target)
            _write_json_fsync(report_path, conversion)

            source_rows = read_jsonl(temporary / "reports" / "source_manifest.jsonl")
            observed_sessions = {
                str(row["source_session_id"])
                for row in source_rows
                if "source_session_id" in row
            }
            if observed_sessions != {receipt.source_session_id}:
                raise RuntimeError(
                    "Fragment source Session does not match frozen selection: "
                    f"expected={receipt.source_session_id!r}, "
                    f"observed={sorted(observed_sessions)!r}"
                )
            revalidated = timed(
                "stage_revalidate", lambda: validate_stage(receipt.stage_dir)
            )
            if revalidated != receipt:
                raise ValueError("StageReceipt changed during conversion")
            fragment = _fragment_value(
                receipt,
                recipe,
                task,
                collection_type,
                outcome,
                station_id,
                pipeline.cleaning.quality,
                conversion,
                pipeline.validation,
                source_rows,
                repo_id,
                (clock or _utc_now)(),
            )
            _write_json_fsync(temporary / "fragment.json", fragment)
            _write_json_fsync(
                temporary / "COMMITTED.json",
                {
                    "schema_version": FRAGMENT_SCHEMA_VERSION,
                    "episode_id": episode_id,
                    "fragment_status": ConversionStatus.COMMITTED.value,
                    "committed_at": fragment["committed_at"],
                },
            )
            phases.append(("finalize", max(time.monotonic() - finalize_started, 0.0)))
    except _Excluded as excluded:
        revalidated = timed(
            "stage_revalidate", lambda: validate_stage(receipt.stage_dir)
        )
        if revalidated != receipt:
            raise ValueError("StageReceipt changed during conversion")
        return excluded.result

    return EpisodeConversionResult(
        episode_id=episode_id,
        status=ConversionStatus.COMMITTED,
        fragment_dir=fragment_target,
        segment_count=int(fragment["segment_count"]),
        frame_count=int(fragment["frame_count"]),
        phase_seconds=tuple(phases),
    )


def _excluded_result(
    receipt: StageReceipt,
    excluded_episodes: tuple[dict[str, object], ...],
    phase_seconds: tuple[tuple[str, float], ...] = (),
) -> EpisodeConversionResult:
    if len(excluded_episodes) != 1:
        raise RuntimeError("single Episode selector must return one exclusion")
    exclusion = excluded_episodes[0]
    if exclusion.get("episode_id") != receipt.episode_id:
        raise RuntimeError("selector exclusion references another Episode")
    reason = exclusion.get("reason")
    if not isinstance(reason, str) or not _REASON_COMPONENT.fullmatch(reason):
        raise RuntimeError("selector exclusion has no stable reason code")
    return EpisodeConversionResult(
        episode_id=receipt.episode_id,
        status=ConversionStatus.EXCLUDED,
        fragment_dir=None,
        segment_count=0,
        frame_count=0,
        reason_code=f"selection/{reason}",
        phase_seconds=phase_seconds,
    )


def _rewrite_report_paths(report: dict[str, object], target: Path) -> None:
    report["source_selection_dir"] = str((target / "selection").resolve())
    report["output_root"] = str((target / "lerobot").resolve())
    if "source_manifest" in report:
        report["source_manifest"] = str(
            (target / "reports" / "source_manifest.jsonl").resolve()
        )


def _fragment_value(
    receipt: StageReceipt,
    recipe: DatasetPipelineRecipe,
    task: str,
    collection_type: str,
    outcome: str,
    station_id: str,
    quality: dict[str, object],
    conversion: dict[str, object],
    validation: dict[str, object],
    source_rows: list[dict[str, object]],
    repo_id: str,
    committed_at: datetime,
) -> dict[str, object]:
    contract_keys = (
        "openpi_commit",
        "lerobot_commit",
        "fps",
        "mode",
        "image_size",
        "image_color",
        "state_action_order",
        "state_action_version",
        "filter_version",
        "gripper_calibration",
        "sampling_contract",
    )
    contracts = {key: conversion[key] for key in contract_keys if key in conversion}
    return {
        "schema_version": FRAGMENT_SCHEMA_VERSION,
        "status": ConversionStatus.COMMITTED.value,
        "episode_id": receipt.episode_id,
        "source_dir": str(receipt.source_dir),
        "source_identity": {
            "mcap": _identity_value(receipt.mcap),
            "metadata": _identity_value(receipt.metadata),
        },
        "collection_type": collection_type,
        "outcome": outcome,
        "source_station_id": station_id,
        "metadata_task": quality.get("task"),
        "training_task": task,
        "source_session_ids": sorted(
            {
                str(row["source_session_id"])
                for row in source_rows
                if "source_session_id" in row
            }
        ),
        "recipe": {
            "schema_version": recipe.schema_version,
            "name": recipe.name,
            "builder_backend": recipe.builder_backend,
            "gripper_normalization": recipe.gripper_normalization,
            "gripper_contract": recipe.gripper_contract,
            **({"video": recipe.video.as_report()} if recipe.video is not None else {}),
        },
        "repo_id": repo_id,
        "segment_count": int(validation["episodes"]),
        "frame_count": int(validation["frames"]),
        "contracts": contracts,
        "paths": {
            "dataset": "lerobot",
            "selection": "selection",
            "conversion_report": "reports/conversion.json",
            "source_manifest": "reports/source_manifest.jsonl",
        },
        "committed_at": _format_utc(committed_at),
    }


def _identity_value(identity: FileIdentity) -> dict[str, int]:
    return {
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
    }


def _metadata_string(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"staged metadata {key} must be a non-empty string")
    return value


def _metadata_station_id(metadata: dict[str, object]) -> str:
    station = metadata.get("station")
    if not isinstance(station, dict):
        raise ValueError("staged metadata station must be an object")
    value = station.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("staged metadata station.id must be a non-empty string")
    return value


def _write_json_fsync(path: Path, value: dict[str, object]) -> None:
    with path.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Fragment clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
