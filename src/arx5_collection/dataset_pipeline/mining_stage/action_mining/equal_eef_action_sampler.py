from __future__ import annotations

from bisect import bisect_right
import math

from arx5_collection.common.gripper import GripperCalibration
from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    EqualEefPolicy,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    EqualEefSample,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.utils import make_state

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


def _latest_arm(
    samples: tuple[ArmSample, ...],
    stamps: tuple[int, ...],
    tick_ns: int,
    max_age_ns: int,
) -> ArmSample | None:
    index = bisect_right(stamps, tick_ns) - 1
    if index < 0:
        return None
    sample = samples[index]
    age_ns = tick_ns - sample.ref.header_stamp_ns
    return sample if age_ns <= max_age_ns else None


def _latest_frame_group(
    groups: tuple[FrameGroup, ...],
    cutoffs: tuple[int, ...],
    tick_ns: int,
    max_age_ns: int,
) -> FrameGroup | None:
    index = bisect_right(cutoffs, tick_ns) - 1
    if index < 0:
        return None
    group = groups[index]
    age_ns = tick_ns - group.observation_cutoff_ns
    return group if age_ns <= max_age_ns else None


def _eef_distance(start: ArmSample, end: ArmSample) -> float:
    """Distance between recorded EEF xyz values, which are expected in metres."""

    return math.dist(start.eef_xyzrpy[:3], end.eef_xyzrpy[:3])


def build_equal_eef_samples(
    scan: EpisodeScan,
    frame_groups: tuple[FrameGroup, ...],
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
    policy: EqualEefPolicy = EqualEefPolicy(),
) -> tuple[EqualEefSample, ...]:
    """Build a trajectory-indexed sequence from real Header-stamped samples."""

    if not frame_groups or not scan.left_arm or not scan.right_arm:
        return ()
    groups = tuple(sorted(frame_groups, key=lambda group: group.observation_cutoff_ns))
    group_cutoffs = tuple(group.observation_cutoff_ns for group in groups)
    left_arm = tuple(
        sorted(
            scan.left_arm,
            key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence),
        )
    )
    right_arm = tuple(
        sorted(
            scan.right_arm,
            key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence),
        )
    )
    left_stamps = tuple(sample.ref.header_stamp_ns for sample in left_arm)
    right_stamps = tuple(sample.ref.header_stamp_ns for sample in right_arm)
    candidate_ticks = sorted(set(left_stamps) | set(right_stamps))

    selected: list[EqualEefSample] = []
    previous_left: ArmSample | None = None
    previous_right: ArmSample | None = None
    previous_state: tuple[float, ...] | None = None
    previous_tick_ns: int | None = None

    for tick_ns in candidate_ticks:
        group = _latest_frame_group(
            groups,
            group_cutoffs,
            tick_ns,
            policy.image_max_age_ns,
        )
        if group is None:
            continue
        current_left = _latest_arm(
            left_arm, left_stamps, tick_ns, policy.arm_max_age_ns
        )
        current_right = _latest_arm(
            right_arm, right_stamps, tick_ns, policy.arm_max_age_ns
        )
        if current_left is None or current_right is None:
            continue
        state = make_state(current_left, current_right, left_gripper, right_gripper)

        if previous_tick_ns is None:
            reasons = ("initial",)
            delta_time_ns = 0
            left_eef_delta_m = 0.0
            right_eef_delta_m = 0.0
            left_gripper_delta = 0.0
            right_gripper_delta = 0.0
        else:
            assert previous_left is not None
            assert previous_right is not None
            assert previous_state is not None
            delta_time_ns = tick_ns - previous_tick_ns
            left_eef_delta_m = _eef_distance(previous_left, current_left)
            right_eef_delta_m = _eef_distance(previous_right, current_right)
            left_gripper_delta = abs(state[6] - previous_state[6])
            right_gripper_delta = abs(state[13] - previous_state[13])
            reason_list = []
            if max(left_eef_delta_m, right_eef_delta_m) >= policy.eef_distance_m:
                reason_list.append("eef_distance")
            if (
                max(left_gripper_delta, right_gripper_delta)
                >= policy.gripper_delta_threshold
            ):
                reason_list.append("gripper")
            if delta_time_ns >= policy.max_sample_interval_ns:
                reason_list.append("max_interval")
            if not reason_list:
                continue
            reasons = tuple(reason_list)

        selected.append(
            EqualEefSample(
                sample_index=len(selected),
                tick_ns=tick_ns,
                frame_group_id=group.frame_group_id,
                overview_color=group.overview.color,
                left_color=group.left.color,
                right_color=group.right.color,
                left_arm=current_left,
                right_arm=current_right,
                state=state,
                action=state,
                delta_time_ns=delta_time_ns,
                sampling_reasons=reasons,
                left_eef_delta_m=left_eef_delta_m,
                right_eef_delta_m=right_eef_delta_m,
                left_gripper_delta=left_gripper_delta,
                right_gripper_delta=right_gripper_delta,
            )
        )
        previous_left = current_left
        previous_right = current_right
        previous_state = state
        previous_tick_ns = tick_ns

    return tuple(selected)


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    intervals = require_output(context.mining_intervals, "training_interval")
    context.interval_samples = timed(
        unit.type,
        lambda: tuple(
            build_equal_eef_samples(
                interval.scan,
                interval.frame_groups,
                context.recipe.gripper,
                context.recipe.gripper,
                context.recipe.selection,
            )
            for interval in intervals
        ),
    )
