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

from arx5_collection.artifacts import read_json
from arx5_collection.artifacts import read_jsonl
from arx5_collection.atomic import staged_directory
from arx5_collection.cleaning.pipeline import clean_episode
from arx5_collection.cleaning.reader import load_metadata
from arx5_collection.dagger_dataset.pipeline import classify_dagger_episode
from arx5_collection.dagger_dataset.selection import select_equal_eef_dagger_dataset
from arx5_collection.pi05_dataset.exporter import export_lerobot
from arx5_collection.pi05_dataset.selection_pipeline import select_equal_eef_dataset
from arx5_collection.pi05_dataset.validate import validate_lerobot

from .models import ConversionStatus
from .models import EpisodeConversionResult
from .models import FileIdentity
from .models import StageReceipt
from .recipe import Pi05ConversionRecipe
from .source import validate_stage


FRAGMENT_SCHEMA_VERSION = 2
_REASON_COMPONENT = re.compile(r"^[a-z][a-z0-9_]*$")
T = TypeVar("T")


class _Excluded(RuntimeError):
    def __init__(self, result: EpisodeConversionResult) -> None:
        self.result = result


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
        segment_count=_non_negative_int(fragment.get("segment_count"), "segment_count"),
        frame_count=_non_negative_int(fragment.get("frame_count"), "frame_count"),
    )


def convert_episode_fragment(
    receipt: StageReceipt,
    fragment_target: Path,
    recipe: Pi05ConversionRecipe,
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

    metadata = timed("metadata", lambda: load_metadata(receipt.stage_dir))
    episode_id = _metadata_string(metadata, "episode_id")
    if episode_id != receipt.episode_id:
        raise ValueError("staged metadata episode_id does not match StageReceipt")
    collection_type = metadata.get("collection_type", "demonstration")
    if not isinstance(collection_type, str):
        raise ValueError("staged metadata collection_type must be a string")
    outcome = _metadata_string(metadata, "outcome")
    station_id = _metadata_station_id(metadata)
    if collection_type not in {"demonstration", "dagger"}:
        raise ValueError(f"unsupported collection_type: {collection_type!r}")
    if collection_type == "dagger" and outcome == "fail":
        if "dagger_fail" not in receipt.source_dir.parts:
            raise ValueError("DAgger fail Episode must originate under dagger_fail/")

    try:
        with staged_directory(fragment_target) as temporary:
            audit_root = temporary / "audit"
            cleaning = timed(
                "clean",
                lambda: clean_episode(
                    receipt.stage_dir,
                    audit_root,
                    recipe.cleaning,
                ),
            )
            if collection_type == "dagger":
                timed(
                    "classify",
                    lambda: classify_dagger_episode(receipt.stage_dir, audit_root),
                )
                selection = timed(
                    "select",
                    lambda: select_equal_eef_dagger_dataset(
                        [receipt.stage_dir],
                        audit_root,
                        temporary,
                        task,
                        recipe.gripper,
                        recipe.gripper,
                        recipe.selection,
                        source_session_ids={episode_id: receipt.source_session_id},
                    ),
                )
            else:
                selection = timed(
                    "select",
                    lambda: select_equal_eef_dataset(
                        [receipt.stage_dir],
                        audit_root,
                        temporary,
                        task,
                        recipe.gripper,
                        recipe.gripper,
                        recipe.selection,
                        source_session_ids={episode_id: receipt.source_session_id},
                    ),
                )
            if not selection.episodes:
                raise _Excluded(
                    _excluded_result(
                        receipt,
                        selection.excluded_episodes,
                        tuple(phases),
                    )
                )
            if selection.output_dir != temporary / "selection":
                raise RuntimeError("selector wrote outside the Fragment workspace")

            dataset_root = timed(
                "export",
                lambda: export_lerobot(
                    receipt.stage_dir,
                    selection.output_dir,
                    temporary,
                    repo_id,
                    dataset_root=temporary / "lerobot",
                    video=recipe.video,
                    phase_reporter=lambda name, seconds: phases.append(
                        (f"export_{name}", seconds)
                    ),
                ),
            )
            validation = timed(
                "validate",
                lambda: validate_lerobot(
                    dataset_root,
                    repo_id,
                    action_horizon=recipe.selection.action_horizon,
                    expected_task=task,
                ),
            )
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
            fragment = _fragment_value(
                receipt,
                recipe,
                task,
                collection_type,
                outcome,
                station_id,
                cleaning.quality,
                conversion,
                validation,
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
    recipe: Pi05ConversionRecipe,
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
    contracts = {
        key: conversion[key]
        for key in contract_keys
        if key in conversion
    }
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
            **(
                {"video": recipe.video.as_report()}
                if recipe.video is not None
                else {}
            ),
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


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Fragment {label} must be a non-negative integer")
    return value
