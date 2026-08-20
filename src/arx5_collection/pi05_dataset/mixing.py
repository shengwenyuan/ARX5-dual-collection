from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Mapping

from arx5_collection.artifacts import read_json
from arx5_collection.artifacts import read_jsonl
from arx5_collection.artifacts import write_json
from arx5_collection.artifacts import write_jsonl
from arx5_collection.atomic import staged_directory


_CONTRACT_KEYS = (
    "filter_version",
    "state_action_version",
    "gripper_calibration",
    "sampling_contract",
)


def _source_rows(selection_dir: Path) -> list[dict[str, object]]:
    path = selection_dir / "source_manifest.jsonl"
    if path.is_file():
        return read_jsonl(path)
    return [
        {
            "schema_version": 1,
            "segment_id": row["segment_id"],
            "source_episode_id": row["source_episode_id"],
            "split_group": row["source_episode_id"],
            "collection_type": "demonstration",
            "training_class": "demonstration",
            "intervention_id": None,
            "authority_segment_id": None,
            "source_started_bag_timestamp_ns": None,
            "source_ended_bag_timestamp_ns": None,
            "sample_weight": 1.0,
        }
        for row in read_jsonl(selection_dir / "segments.jsonl")
    ]


def _selection_tasks(selection_dir: Path) -> frozenset[str]:
    tasks = frozenset(
        str(row["task"]).strip()
        for row in read_jsonl(selection_dir / "segments.jsonl")
    )
    if not tasks or "" in tasks:
        raise ValueError(f"selection has no valid task contract: {selection_dir}")
    return tasks


def mix_selections(
    inputs: Mapping[str, Path],
    output_root: Path,
    weights: Mapping[str, float] | None = None,
) -> Path:
    """Merge compatible selection manifests without copying or repeating samples."""

    if len(inputs) < 2:
        raise ValueError("selection mixing requires at least two named inputs")
    configured_weights = dict(weights or {})
    unknown_weights = set(configured_weights) - set(inputs)
    if unknown_weights:
        raise ValueError(f"weights reference unknown inputs: {sorted(unknown_weights)}")
    if any(value <= 0 for value in configured_weights.values()):
        raise ValueError("mixture weights must be positive")

    reports = {name: read_json(path / "selection.json") for name, path in inputs.items()}
    first_name = next(iter(inputs))
    baseline = reports[first_name]
    baseline_tasks = _selection_tasks(inputs[first_name])
    for name, report in reports.items():
        for key in _CONTRACT_KEYS:
            if report.get(key) != baseline.get(key):
                raise ValueError(f"selection contract mismatch for {name}: {key}")
        if _selection_tasks(inputs[name]) != baseline_tasks:
            raise ValueError(f"selection contract mismatch for {name}: task")

    sample_rows: list[dict[str, object]] = []
    segment_rows: list[dict[str, object]] = []
    source_rows: list[dict[str, object]] = []
    excluded = []
    seen_samples: set[tuple[str, int]] = set()
    seen_segments: set[str] = set()
    for name, selection_dir in inputs.items():
        weight = float(configured_weights.get(name, 1.0))
        for sample in read_jsonl(selection_dir / "sample_index.jsonl"):
            key = (str(sample["source_episode_id"]), int(sample["source_sample_index"]))
            if key in seen_samples:
                raise ValueError(f"duplicate source sample across selections: {key}")
            seen_samples.add(key)
            sample_rows.append(sample)
        for segment in read_jsonl(selection_dir / "segments.jsonl"):
            segment_id = str(segment["segment_id"])
            if segment_id in seen_segments:
                raise ValueError(f"duplicate segment id across selections: {segment_id}")
            seen_segments.add(segment_id)
            segment_rows.append(segment)
        for row in _source_rows(selection_dir):
            row = dict(row)
            row["mixture_source"] = name
            row["sample_weight"] = weight
            source_rows.append(row)
        excluded.extend(
            {"mixture_source": name, **dict(row)}
            for row in report.get("excluded_episodes", [])
        )

    manifest_segments = {str(row["segment_id"]) for row in source_rows}
    if manifest_segments != seen_segments:
        raise ValueError("source manifest does not cover the merged segments exactly")
    composition = Counter(str(row["collection_type"]) for row in source_rows)
    report = dict(baseline)
    report.update(
        selection_kind="mixed",
        source_episode_count=len(
            {str(row["source_episode_id"]) for row in source_rows}
        ),
        selected_source_episode_count=len(
            {str(row["source_episode_id"]) for row in source_rows}
        ),
        excluded_episodes=excluded,
        sample_count=len(sample_rows),
        eligible_sample_count=sum(
            sample.get("training_eligible") is True for sample in sample_rows
        ),
        segment_count=len(segment_rows),
        source_composition=dict(sorted(composition.items())),
        mixture={
            name: {
                "selection_dir": str(path.resolve()),
                "sample_weight": float(configured_weights.get(name, 1.0)),
            }
            for name, path in inputs.items()
        },
        weighting_applied=False,
        tasks=sorted(baseline_tasks),
    )
    target = output_root / "selection"
    with staged_directory(target) as temporary:
        write_jsonl(temporary / "sample_index.jsonl", sample_rows)
        write_jsonl(temporary / "segments.jsonl", segment_rows)
        write_jsonl(temporary / "source_manifest.jsonl", source_rows)
        write_json(temporary / "selection.json", report)
    return target
