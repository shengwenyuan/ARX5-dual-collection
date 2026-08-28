from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arx5_collection.artifacts import ArmRefArtifact
from arx5_collection.artifacts import FrameArmsArtifact
from arx5_collection.artifacts import FrameGroupArtifact
from arx5_collection.artifacts import FrameImagesArtifact
from arx5_collection.artifacts import ImagePairArtifact
from arx5_collection.artifacts import message_ref_to_artifact
from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import CleaningPolicy
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.cleaning.models import ImagePair
from arx5_collection.cleaning.models import LEFT_ARM_TOPIC
from arx5_collection.cleaning.models import RIGHT_ARM_TOPIC
from arx5_collection.cleaning.models import required_topics
from arx5_collection.cleaning.pairing import PairingResult
from arx5_collection.cleaning.pairing import build_frame_groups
from arx5_collection.cleaning.reader import load_metadata
from arx5_collection.cleaning.reader import read_episode_scan
from arx5_collection.cleaning.store import write_cleaning_artifacts
from arx5_collection.cleaning.timeline import audit_timeline


POLICY_VERSION = "arx5-cleaning-v1"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CleaningResult:
    quality: dict[str, Any]
    frame_groups: tuple[FrameGroup, ...]
    output_dir: Path | None = None


def _value_stats(samples: tuple[ArmSample, ...]) -> dict[str, Any]:
    if not samples:
        return {"count": 0, "joint_min": [], "joint_max": [], "gripper_min": None, "gripper_max": None}
    joint_min = list(samples[0].joint_positions)
    joint_max = list(samples[0].joint_positions)
    gripper_min = samples[0].gripper_position
    gripper_max = samples[0].gripper_position
    for sample in samples[1:]:
        joint_min = [min(old, value) for old, value in zip(joint_min, sample.joint_positions)]
        joint_max = [max(old, value) for old, value in zip(joint_max, sample.joint_positions)]
        gripper_min = min(gripper_min, sample.gripper_position)
        gripper_max = max(gripper_max, sample.gripper_position)
    return {
        "count": len(samples),
        "joint_min": joint_min,
        "joint_max": joint_max,
        "joint_range": [high - low for low, high in zip(joint_min, joint_max)],
        "gripper_min": gripper_min,
        "gripper_max": gripper_max,
        "gripper_range": gripper_max - gripper_min,
    }


def _pair(pair: ImagePair) -> ImagePairArtifact:
    return ImagePairArtifact(
        stamp_ns=pair.stamp_ns,
        color=message_ref_to_artifact(pair.color),
        depth=(
            None
            if pair.depth is None
            else message_ref_to_artifact(pair.depth)
        ),
    )


def frame_group_to_dict(group: FrameGroup, episode_id: str) -> FrameGroupArtifact:
    images = FrameImagesArtifact(
        overview=_pair(group.overview),
        left=_pair(group.left),
        right=_pair(group.right),
    )
    arms = FrameArmsArtifact(
        left=ArmRefArtifact(
            ref=message_ref_to_artifact(group.left_arm.ref),
            age_ns=group.observation_cutoff_ns - group.left_arm.ref.header_stamp_ns,
        ),
        right=ArmRefArtifact(
            ref=message_ref_to_artifact(group.right_arm.ref),
            age_ns=group.observation_cutoff_ns - group.right_arm.ref.header_stamp_ns,
        ),
    )
    return FrameGroupArtifact(
        schema_version=SCHEMA_VERSION,
        episode_id=episode_id,
        frame_group_id=group.frame_group_id,
        observation_cutoff_ns=group.observation_cutoff_ns,
        images=images,
        arms=arms,
    )


def _grade(pairing: PairingResult, timeline_has_warnings: bool, policy: CleaningPolicy) -> str:
    if not pairing.frame_groups or pairing.coverage < policy.grade_b_coverage:
        return "C"
    if timeline_has_warnings or pairing.coverage < policy.grade_a_coverage:
        return "B"
    return "A"


def inspect_episode(
    episode_dir: Path,
    policy: CleaningPolicy = CleaningPolicy(),
) -> CleaningResult:
    metadata = load_metadata(episode_dir)
    scan = read_episode_scan(episode_dir)
    timeline = {
        topic: audit_timeline(scan.refs_by_topic[topic]).to_dict()
        for topic in required_topics(scan.capture_profile)
    }
    pairing = build_frame_groups(scan, policy)
    timeline_has_errors = any(
        stats["duplicate_count"]
        or stats["non_monotonic_count"]
        for stats in timeline.values()
    )
    excessive_gap_topics = [
        topic
        for topic, stats in timeline.items()
        if stats["max_positive_gap_ns"]
        > (policy.arm_gap_warning_ns if topic in (LEFT_ARM_TOPIC, RIGHT_ARM_TOPIC) else policy.camera_gap_warning_ns)
    ]
    issues = []
    if timeline_has_errors:
        issues.append("one or more streams contain duplicate/non-monotonic Header timestamps")
    for topic in excessive_gap_topics:
        issues.append(
            f"stream {topic} has a {timeline[topic]['max_positive_gap_ns']} ns gap exceeding the warning threshold"
        )
    if pairing.rejected_cross_camera:
        issues.append(f"{pairing.rejected_cross_camera} overview pairs failed cross-camera tolerance")
    if pairing.rejected_arm_age:
        issues.append(f"{pairing.rejected_arm_age} frame groups failed arm age tolerance")
    for role, stats in pairing.camera_stats.items():
        if stats.color_only_count or stats.depth_only_count:
            issues.append(
                f"camera {role} has {stats.color_only_count} color-only and "
                f"{stats.depth_only_count} depth-only frames"
            )
    quality = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "episode_id": metadata["episode_id"],
        "source": {
            "episode_dir": str(episode_dir.resolve()),
            "mcap_path": str((episode_dir / "episode.mcap").resolve()),
        },
        "outcome": metadata["outcome"],
        "task": metadata["task"],
        "capture_profile": scan.capture_profile.value,
        "timeline": timeline,
        "camera_pairing": {
            role: stats.to_dict() for role, stats in pairing.camera_stats.items()
        },
        "common_interval": {
            "start_ns": pairing.common_start_ns,
            "end_ns": pairing.common_end_ns,
        },
        "frame_grouping": {
            "eligible_overview_pairs": pairing.eligible_overview_pairs,
            "valid_frame_groups": len(pairing.frame_groups),
            "coverage": pairing.coverage,
            "rejected_cross_camera": pairing.rejected_cross_camera,
            "rejected_arm_age": pairing.rejected_arm_age,
        },
        "arm_values": {
            "left": _value_stats(scan.left_arm),
            "right": _value_stats(scan.right_arm),
            "discarded_nonfinite": {
                "left": len(scan.refs_by_topic[LEFT_ARM_TOPIC]) - len(scan.left_arm),
                "right": len(scan.refs_by_topic[RIGHT_ARM_TOPIC]) - len(scan.right_arm),
            },
        },
        "issues": issues,
        "grade": _grade(pairing, timeline_has_errors or bool(excessive_gap_topics), policy),
    }
    return CleaningResult(quality=quality, frame_groups=pairing.frame_groups)


def clean_episode(
    episode_dir: Path,
    output_root: Path,
    policy: CleaningPolicy = CleaningPolicy(),
) -> CleaningResult:
    result = inspect_episode(episode_dir, policy)
    output_dir = write_cleaning_artifacts(
        output_root,
        str(result.quality["episode_id"]),
        result.quality,
        [frame_group_to_dict(group, str(result.quality["episode_id"])) for group in result.frame_groups],
    )
    return CleaningResult(result.quality, result.frame_groups, output_dir)
