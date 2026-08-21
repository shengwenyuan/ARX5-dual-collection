from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arx5_collection.artifacts import ExcludedEpisodeArtifact
from arx5_collection.artifacts import read_json
from arx5_collection.cleaning.reader import read_episode_scan
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.artifact_codec import load_frame_groups
from arx5_collection.pi05_dataset.artifact_codec import write_equal_eef_selection_artifacts
from arx5_collection.pi05_dataset.artifact_codec import write_selection_artifacts
from arx5_collection.pi05_dataset.eef_selection import build_equal_eef_samples
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy
from arx5_collection.pi05_dataset.selection import Pi05Policy
from arx5_collection.pi05_dataset.selection import Pi05Sample
from arx5_collection.pi05_dataset.selection import Pi05Segment
from arx5_collection.pi05_dataset.selection import build_samples
from arx5_collection.pi05_dataset.selection import select_nonidle_segments
from arx5_collection.pi05_dataset.provenance import SegmentProvenance
from arx5_collection.pi05_dataset.provenance import derive_source_session_id


@dataclass(frozen=True, slots=True)
class EpisodeSelection:
    episode_id: str
    task: str
    samples: tuple[Pi05Sample, ...]
    segments: tuple[Pi05Segment, ...]
    segment_provenance: tuple[SegmentProvenance, ...] = ()
    source_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("task must not be empty")
        if self.source_session_id is not None and not self.source_session_id:
            raise ValueError("source Session id must not be empty")
        if self.segment_provenance and len(self.segment_provenance) != len(self.segments):
            raise ValueError("segment provenance must match selected segments")


@dataclass(frozen=True, slots=True)
class DatasetSelection:
    episodes: tuple[EpisodeSelection, ...]
    excluded_episodes: tuple[ExcludedEpisodeArtifact, ...]
    output_dir: Path | None = None


SampleBuilder = Callable[..., tuple[Pi05Sample, ...]]
ArtifactWriter = Callable[..., Path]


def _select_dataset(
    episode_dirs: list[Path],
    audit_root: Path,
    output_root: Path,
    task: str,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
    policy: Pi05Policy | EqualEefPolicy,
    sample_builder: SampleBuilder,
    artifact_writer: ArtifactWriter,
) -> DatasetSelection:
    if not task.strip():
        raise ValueError("task must not be empty")
    selected = []
    excluded: list[ExcludedEpisodeArtifact] = []
    for episode_dir in episode_dirs:
        episode_id = episode_dir.name
        quality = read_json(audit_root / episode_id / "quality.json")
        reason = None
        if quality.get("outcome") != "success":
            reason = f"outcome_{quality.get('outcome', 'missing')}"
        elif quality.get("grade") == "C":
            reason = "quality_grade_C"
        if reason is not None:
            excluded.append(ExcludedEpisodeArtifact(episode_id=episode_id, reason=reason))
            continue
        scan = read_episode_scan(episode_dir)
        groups = load_frame_groups(audit_root / episode_id / "frame_index.jsonl", scan)
        if groups and (
            groups[-1].observation_cutoff_ns - groups[0].observation_cutoff_ns
        ) / 1e9 > policy.max_episode_duration_s:
            excluded.append(ExcludedEpisodeArtifact(episode_id=episode_id, reason="episode_too_long"))
            continue
        samples = sample_builder(scan, groups, left_gripper, right_gripper, policy)
        segments = select_nonidle_segments(samples, policy)
        if not segments:
            excluded.append(
                ExcludedEpisodeArtifact(episode_id=episode_id, reason="no_valid_motion_segment")
            )
            continue
        selected.append(
            EpisodeSelection(
                episode_id,
                task,
                samples,
                segments,
                source_session_id=derive_source_session_id(episode_dir),
            )
        )
    result = DatasetSelection(tuple(selected), tuple(excluded))
    output_dir = artifact_writer(output_root, result, policy, left_gripper, right_gripper)
    return DatasetSelection(result.episodes, result.excluded_episodes, output_dir)


def select_dataset(
    episode_dirs: list[Path],
    audit_root: Path,
    output_root: Path,
    task: str,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
    policy: Pi05Policy = Pi05Policy(),
) -> DatasetSelection:
    return _select_dataset(
        episode_dirs,
        audit_root,
        output_root,
        task,
        left_gripper,
        right_gripper,
        policy,
        build_samples,
        write_selection_artifacts,
    )


def select_equal_eef_dataset(
    episode_dirs: list[Path],
    audit_root: Path,
    output_root: Path,
    task: str,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
    policy: EqualEefPolicy = EqualEefPolicy(),
) -> DatasetSelection:
    return _select_dataset(
        episode_dirs,
        audit_root,
        output_root,
        task,
        left_gripper,
        right_gripper,
        policy,
        build_equal_eef_samples,
        write_equal_eef_selection_artifacts,
    )
