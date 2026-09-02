from __future__ import annotations

from pathlib import Path

from arx5_collection.dataset_pipeline.persistence.artifacts import write_json
from arx5_collection.dataset_pipeline.persistence.artifacts import write_jsonl
from arx5_collection.dataset_pipeline.persistence.atomic import staged_directory

from ..models import AuthorityClassification


CLASSIFIER_VERSION = "dagger-authority-v1"
SCHEMA_VERSION = 1


def classification_quality(result: AuthorityClassification) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "episode_id": result.episode_id,
        "valid": result.valid,
        "issues": list(result.issues),
        "episode_monotonic_anchor_ns": result.episode_monotonic_anchor_ns,
        "episode_bag_anchor_ns": result.episode_bag_anchor_ns,
        "bag_anchor_spread_ns": result.bag_anchor_spread_ns,
        "event_count": result.event_count,
        "intervention_count": result.intervention_count,
        "segment_count": len(result.segments),
        "expert_correction_count": len(result.expert_segments),
    }


def segment_rows(result: AuthorityClassification) -> list[dict[str, object]]:
    return [
        {
            "schema_version": SCHEMA_VERSION,
            "classifier_version": CLASSIFIER_VERSION,
            "segment_id": segment.segment_id,
            "source_episode_id": result.episode_id,
            "authority_class": segment.authority_class.value,
            "started_offset_ns": segment.started_offset_ns,
            "ended_offset_ns": segment.ended_offset_ns,
            "started_bag_timestamp_ns": segment.started_bag_timestamp_ns,
            "ended_bag_timestamp_ns": segment.ended_bag_timestamp_ns,
            "intervention_id": segment.intervention_id,
            "complete": segment.complete,
            "training_eligible": segment.training_eligible,
            "exclusion_reason": segment.exclusion_reason,
        }
        for segment in result.segments
    ]


def write_authority_artifacts(
    audit_root: Path,
    result: AuthorityClassification,
) -> Path:
    target = audit_root / result.episode_id / "authority"
    if not (target.parent / "quality.json").is_file():
        raise FileNotFoundError(target.parent / "quality.json")
    with staged_directory(target) as temporary:
        write_json(temporary / "quality.json", classification_quality(result))
        write_jsonl(temporary / "segments.jsonl", segment_rows(result))
    return target
