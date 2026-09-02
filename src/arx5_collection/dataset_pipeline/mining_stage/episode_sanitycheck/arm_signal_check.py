from __future__ import annotations

from arx5_collection.dataset_pipeline.source.models import LEFT_ARM_TOPIC
from arx5_collection.dataset_pipeline.source.models import RIGHT_ARM_TOPIC
from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import EpisodeSanitycheckResult

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


def arm_value_stats(samples: tuple[ArmSample, ...]) -> dict[str, object]:
    if not samples:
        return {
            "count": 0,
            "joint_min": [],
            "joint_max": [],
            "gripper_min": None,
            "gripper_max": None,
        }
    joint_min = list(samples[0].joint_positions)
    joint_max = list(samples[0].joint_positions)
    gripper_min = samples[0].gripper_position
    gripper_max = samples[0].gripper_position
    for sample in samples[1:]:
        joint_min = [
            min(old, value)
            for old, value in zip(joint_min, sample.joint_positions, strict=True)
        ]
        joint_max = [
            max(old, value)
            for old, value in zip(joint_max, sample.joint_positions, strict=True)
        ]
        gripper_min = min(gripper_min, sample.gripper_position)
        gripper_max = max(gripper_max, sample.gripper_position)
    return {
        "count": len(samples),
        "joint_min": joint_min,
        "joint_max": joint_max,
        "joint_range": [
            high - low for low, high in zip(joint_min, joint_max, strict=True)
        ],
        "gripper_min": gripper_min,
        "gripper_max": gripper_max,
        "gripper_range": gripper_max - gripper_min,
    }


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    metadata = require_output(context.metadata, "metadata_check")
    scan = require_output(context.scan, "mcap_check")
    timeline = require_output(context.timeline, "timeline_check")

    def operation() -> EpisodeSanitycheckResult:
        arm_values = {
            "left": arm_value_stats(scan.left_arm),
            "right": arm_value_stats(scan.right_arm),
            "discarded_nonfinite": {
                "left": len(scan.refs_by_topic[LEFT_ARM_TOPIC]) - len(scan.left_arm),
                "right": len(scan.refs_by_topic[RIGHT_ARM_TOPIC]) - len(scan.right_arm),
            },
        }
        return EpisodeSanitycheckResult(
            metadata=metadata,
            scan=scan,
            timeline=timeline,
            timeline_has_errors=context.timeline_has_errors,
            excessive_gap_topics=context.excessive_gap_topics,
            arm_values=arm_values,
            issues=context.issues,
        )

    context.sanitycheck = timed(unit.type, operation)
