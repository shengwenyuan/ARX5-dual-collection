from __future__ import annotations

from pathlib import Path
from typing import Any

from arx5_collection.dataset_pipeline.persistence.artifacts import ArmRefArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import FrameArmsArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import FrameGroupArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import FrameImagesArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import ImagePairArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    message_ref_to_artifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import write_json
from arx5_collection.dataset_pipeline.persistence.artifacts import write_jsonl
from arx5_collection.dataset_pipeline.persistence.atomic import staged_directory
from arx5_collection.dataset_pipeline.source.models import CleaningPolicy
from arx5_collection.dataset_pipeline.source.models import CleaningResult
from arx5_collection.dataset_pipeline.source.models import EpisodeSanitycheckResult
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.source.models import ImagePair
from arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.models import (
    PairingResult,
)
from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


POLICY_VERSION = "arx5-cleaning-v1"
SCHEMA_VERSION = 1


def _pair(pair: ImagePair) -> ImagePairArtifact:
    return ImagePairArtifact(
        stamp_ns=pair.stamp_ns,
        color=message_ref_to_artifact(pair.color),
        depth=(None if pair.depth is None else message_ref_to_artifact(pair.depth)),
    )


def frame_group_to_dict(
    group: FrameGroup,
    episode_id: str,
) -> FrameGroupArtifact:
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


def _grade(
    pairing: PairingResult,
    timeline_has_warnings: bool,
    policy: CleaningPolicy,
) -> str:
    if not pairing.frame_groups or pairing.coverage < policy.grade_b_coverage:
        return "C"
    if timeline_has_warnings or pairing.coverage < policy.grade_a_coverage:
        return "B"
    return "A"


def build_alignment_result(
    sanitycheck: EpisodeSanitycheckResult,
    pairing: PairingResult,
    policy: CleaningPolicy,
) -> CleaningResult:
    metadata = sanitycheck.metadata
    scan = sanitycheck.scan
    issues = list(sanitycheck.issues)
    if pairing.rejected_cross_camera:
        issues.append(
            f"{pairing.rejected_cross_camera} overview pairs failed cross-camera tolerance"
        )
    if pairing.rejected_arm_age:
        issues.append(
            f"{pairing.rejected_arm_age} frame groups failed arm age tolerance"
        )
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
            "episode_dir": str(scan.episode_dir.resolve()),
            "mcap_path": str(scan.mcap_path.resolve()),
        },
        "outcome": metadata["outcome"],
        "task": metadata["task"],
        "capture_profile": scan.capture_profile.value,
        "timeline": sanitycheck.timeline,
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
        "arm_values": sanitycheck.arm_values,
        "issues": issues,
        "grade": _grade(
            pairing,
            sanitycheck.timeline_has_errors or bool(sanitycheck.excessive_gap_topics),
            policy,
        ),
    }
    return CleaningResult(quality=quality, frame_groups=pairing.frame_groups)


def write_cleaning_artifacts(
    output_root: Path,
    episode_id: str,
    quality: dict[str, Any],
    frame_groups: list[dict[str, Any]],
) -> Path:
    target = output_root / episode_id
    with staged_directory(target) as temporary:
        write_json(temporary / "quality.json", quality)
        write_jsonl(temporary / "frame_index.jsonl", frame_groups)
    return target


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    sanitycheck = require_output(context.sanitycheck, "arm_signal_check")
    pairing = require_output(context.pairing, "frame_alignment")

    def operation() -> CleaningResult:
        result = build_alignment_result(
            sanitycheck,
            pairing,
            context.recipe.cleaning,
        )
        episode_id = str(result.quality["episode_id"])
        output_dir = write_cleaning_artifacts(
            context.output_root / "audit",
            episode_id,
            result.quality,
            [frame_group_to_dict(group, episode_id) for group in result.frame_groups],
        )
        return CleaningResult(result.quality, result.frame_groups, output_dir)

    context.cleaning = timed(unit.type, operation)
