from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.actions import make_state
from arx5_collection.pi05_dataset.openpi_contract import ACTION_HORIZON
from arx5_collection.pi05_dataset.openpi_contract import DATASET_FPS
from arx5_collection.pi05_dataset.selection import Pi05Sample


@dataclass(frozen=True, slots=True)
class EqualEefPolicy:
    eef_distance_m: float = 0.005
    gripper_delta_threshold: float = 0.02
    max_sample_interval_ns: int = 100_000_000
    image_max_age_ns: int = 40_000_000
    arm_max_age_ns: int = 2_000_000
    action_horizon: int = ACTION_HORIZON
    nominal_fps: int = DATASET_FPS
    idle_delta_threshold: float = 1e-3
    min_idle_frames: int = 24
    min_motion_frames: int = 54
    trim_segment_end_frames: int = 34
    max_episode_duration_s: float = 180.0

    def __post_init__(self) -> None:
        if self.eef_distance_m <= 0:
            raise ValueError("eef_distance_m must be positive")
        if self.gripper_delta_threshold <= 0:
            raise ValueError("gripper_delta_threshold must be positive")
        if self.max_sample_interval_ns <= 0:
            raise ValueError("max_sample_interval_ns must be positive")
        if self.image_max_age_ns < 0 or self.arm_max_age_ns < 0:
            raise ValueError("sample age limits must not be negative")
        if self.action_horizon <= 0 or self.nominal_fps <= 0:
            raise ValueError("OpenPI index parameters must be positive")
        if any(
            value < 0
            for value in (
                self.idle_delta_threshold,
                self.min_idle_frames,
                self.min_motion_frames,
                self.trim_segment_end_frames,
                self.max_episode_duration_s,
            )
        ):
            raise ValueError("segment policy values must not be negative")


@dataclass(frozen=True, slots=True)
class EqualEefSample(Pi05Sample):
    delta_time_ns: int = 0
    sampling_reasons: tuple[str, ...] = ()
    left_eef_delta_m: float = 0.0
    right_eef_delta_m: float = 0.0
    left_gripper_delta: float = 0.0
    right_gripper_delta: float = 0.0

    def __post_init__(self) -> None:
        Pi05Sample.__post_init__(self)
        if self.delta_time_ns < 0:
            raise ValueError("delta_time_ns must not be negative")


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
        sorted(scan.left_arm, key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence))
    )
    right_arm = tuple(
        sorted(scan.right_arm, key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence))
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
        current_left = _latest_arm(left_arm, left_stamps, tick_ns, policy.arm_max_age_ns)
        current_right = _latest_arm(right_arm, right_stamps, tick_ns, policy.arm_max_age_ns)
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
            if max(left_gripper_delta, right_gripper_delta) >= policy.gripper_delta_threshold:
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
