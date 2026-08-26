from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping

from arx5_collection.artifacts import ExcludedEpisodeArtifact
from arx5_collection.artifacts import read_json
from arx5_collection.artifacts import read_jsonl
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.cleaning.reader import read_episode_scan
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.artifact_codec import load_frame_groups
from arx5_collection.pi05_dataset.artifact_codec import write_equal_eef_selection_artifacts
from arx5_collection.pi05_dataset.eef_selection import build_equal_eef_samples
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy
from arx5_collection.pi05_dataset.provenance import SegmentProvenance
from arx5_collection.pi05_dataset.provenance import derive_source_session_id
from arx5_collection.pi05_dataset.selection import Pi05Sample
from arx5_collection.pi05_dataset.selection import Pi05Segment
from arx5_collection.pi05_dataset.selection import select_nonidle_segments
from arx5_collection.pi05_dataset.selection_pipeline import DatasetSelection
from arx5_collection.pi05_dataset.selection_pipeline import EpisodeSelection


def _inside(start_ns: int, end_ns: int, timestamp_ns: int) -> bool:
    return start_ns <= timestamp_ns < end_ns


def _slice_scan(scan: EpisodeScan, start_ns: int, end_ns: int) -> EpisodeScan:
    return replace(
        scan,
        left_arm=tuple(
            sample
            for sample in scan.left_arm
            if _inside(start_ns, end_ns, sample.ref.bag_timestamp_ns)
        ),
        right_arm=tuple(
            sample
            for sample in scan.right_arm
            if _inside(start_ns, end_ns, sample.ref.bag_timestamp_ns)
        ),
    )


def _slice_groups(
    groups: tuple[FrameGroup, ...],
    start_ns: int,
    end_ns: int,
) -> tuple[FrameGroup, ...]:
    return tuple(
        group
        for group in groups
        if _inside(start_ns, end_ns, group.overview.color.bag_timestamp_ns)
    )


def _renumber(
    samples: tuple[Pi05Sample, ...],
    segments: tuple[Pi05Segment, ...],
    sample_offset: int,
    segment_offset: int,
) -> tuple[tuple[Pi05Sample, ...], tuple[Pi05Segment, ...]]:
    replaced = tuple(
        replace(sample, sample_index=sample_offset + index)
        for index, sample in enumerate(samples)
    )
    by_local_index = {
        original.sample_index: current
        for original, current in zip(samples, replaced)
    }
    new_segments = tuple(
        Pi05Segment(
            segment_index=segment_offset + index,
            start_sample_index=by_local_index[segment.samples[0].sample_index].sample_index,
            end_sample_index=(
                by_local_index[segment.samples[-1].sample_index].sample_index + 1
            ),
            samples=tuple(
                by_local_index[sample.sample_index] for sample in segment.samples
            ),
        )
        for index, segment in enumerate(segments)
    )
    return replaced, new_segments


def select_equal_eef_dagger_dataset(
    episode_dirs: list[Path],
    audit_root: Path,
    output_root: Path,
    task: str,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
    policy: EqualEefPolicy = EqualEefPolicy(),
    *,
    source_session_ids: Mapping[str, str] | None = None,
) -> DatasetSelection:
    """Apply the unchanged v2 recipe independently inside complete corrections."""

    if not task.strip():
        raise ValueError("task must not be empty")
    selected = []
    excluded: list[ExcludedEpisodeArtifact] = []
    for episode_dir in episode_dirs:
        episode_id = episode_dir.name
        quality = read_json(audit_root / episode_id / "quality.json")
        authority_quality = read_json(
            audit_root / episode_id / "authority" / "quality.json"
        )
        reason = None
        if quality.get("outcome") not in {"success", "fail"}:
            reason = f"outcome_{quality.get('outcome', 'missing')}"
        elif quality.get("grade") == "C":
            reason = "quality_grade_c"
        elif not authority_quality.get("valid"):
            reason = "invalid_authority_timeline"
        if reason is not None:
            excluded.append(ExcludedEpisodeArtifact(episode_id=episode_id, reason=reason))
            continue
        correction_rows = [
            row
            for row in read_jsonl(
                audit_root / episode_id / "authority" / "segments.jsonl"
            )
            if row.get("authority_class") == "expert_correction"
            and row.get("training_eligible") is True
        ]
        if not correction_rows:
            excluded.append(
                ExcludedEpisodeArtifact(episode_id=episode_id, reason="no_complete_correction")
            )
            continue
        scan = read_episode_scan(episode_dir)
        groups = load_frame_groups(
            audit_root / episode_id / "frame_index.jsonl",
            scan,
        )
        episode_samples: list[Pi05Sample] = []
        episode_segments: list[Pi05Segment] = []
        provenance: list[SegmentProvenance] = []
        for correction in correction_rows:
            start_ns = int(correction["started_bag_timestamp_ns"])
            end_ns = int(correction["ended_bag_timestamp_ns"])
            correction_scan = _slice_scan(scan, start_ns, end_ns)
            correction_groups = _slice_groups(groups, start_ns, end_ns)
            if correction_groups and (
                correction_groups[-1].observation_cutoff_ns
                - correction_groups[0].observation_cutoff_ns
            ) / 1e9 > policy.max_episode_duration_s:
                continue
            local_samples = build_equal_eef_samples(
                correction_scan,
                correction_groups,
                left_gripper,
                right_gripper,
                policy,
            )
            local_segments = select_nonidle_segments(local_samples, policy)
            renumbered_samples, renumbered_segments = _renumber(
                local_samples,
                local_segments,
                len(episode_samples),
                len(episode_segments),
            )
            episode_samples.extend(renumbered_samples)
            episode_segments.extend(renumbered_segments)
            provenance.extend(
                SegmentProvenance(
                    collection_type="dagger",
                    training_class="expert_correction",
                    intervention_id=int(correction["intervention_id"]),
                    authority_segment_id=str(correction["segment_id"]),
                    source_started_bag_timestamp_ns=start_ns,
                    source_ended_bag_timestamp_ns=end_ns,
                )
                for _ in renumbered_segments
            )
        if not episode_segments:
            excluded.append(
                ExcludedEpisodeArtifact(
                    episode_id=episode_id,
                    reason="no_valid_correction_motion_segment",
                )
            )
            continue
        selected.append(
            EpisodeSelection(
                episode_id=episode_id,
                task=task,
                samples=tuple(episode_samples),
                segments=tuple(episode_segments),
                segment_provenance=tuple(provenance),
                source_session_id=(
                    source_session_ids[episode_id]
                    if source_session_ids is not None
                    else derive_source_session_id(episode_dir)
                ),
            )
        )
    result = DatasetSelection(tuple(selected), tuple(excluded))
    output_dir = write_equal_eef_selection_artifacts(
        output_root,
        result,
        policy,
        left_gripper,
        right_gripper,
    )
    return DatasetSelection(result.episodes, result.excluded_episodes, output_dir)
