from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.actions import make_state


@dataclass(frozen=True, slots=True)
class Pi05Policy:
    fps: int = 50
    action_horizon: int = 50
    image_max_age_ns: int = 40_000_000
    arm_max_age_ns: int = 2_000_000
    idle_delta_threshold: float = 1e-3
    min_idle_frames: int = 24
    min_motion_frames: int = 54
    trim_segment_end_frames: int = 34
    max_episode_duration_s: float = 180.0

    @property
    def tick_period_ns(self) -> int:
        return 1_000_000_000 // self.fps

    def __post_init__(self) -> None:
        if self.fps <= 0 or self.action_horizon <= 0:
            raise ValueError("fps and action_horizon must be positive")
        if any(
            value < 0
            for value in (
                self.image_max_age_ns,
                self.arm_max_age_ns,
                self.idle_delta_threshold,
                self.min_idle_frames,
                self.min_motion_frames,
                self.trim_segment_end_frames,
                self.max_episode_duration_s,
            )
        ):
            raise ValueError("pi05 policy values must not be negative")


@dataclass(frozen=True, slots=True)
class Pi05Sample:
    sample_index: int
    tick_ns: int
    frame_group_id: int
    overview_color: MessageRef
    left_color: MessageRef
    right_color: MessageRef
    left_arm: ArmSample
    right_arm: ArmSample
    state: tuple[float, ...]
    action: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.state) != 14 or len(self.action) != 14:
            raise ValueError("π0.5 ARX state and action must be 14-dimensional")


@dataclass(frozen=True, slots=True)
class Pi05Segment:
    segment_index: int
    start_sample_index: int
    end_sample_index: int
    samples: tuple[Pi05Sample, ...]


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
    return sample if 0 <= age_ns <= max_age_ns else None


def build_samples(
    scan: EpisodeScan,
    frame_groups: tuple[FrameGroup, ...],
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
    policy: Pi05Policy = Pi05Policy(),
) -> tuple[Pi05Sample, ...]:
    if not frame_groups:
        return ()
    groups = tuple(sorted(frame_groups, key=lambda group: group.observation_cutoff_ns))
    group_stamps = tuple(group.observation_cutoff_ns for group in groups)
    left_arm = tuple(
        sorted(scan.left_arm, key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence))
    )
    right_arm = tuple(
        sorted(scan.right_arm, key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence))
    )
    left_stamps = tuple(sample.ref.header_stamp_ns for sample in left_arm)
    right_stamps = tuple(sample.ref.header_stamp_ns for sample in right_arm)

    period = policy.tick_period_ns
    first_tick = ((group_stamps[0] + period - 1) // period) * period
    final_tick = group_stamps[-1]
    samples: list[Pi05Sample] = []
    for tick in range(first_tick, final_tick + 1, period):
        group_index = bisect_right(group_stamps, tick) - 1
        if group_index < 0:
            continue
        group = groups[group_index]
        measurement_age_ns = tick - group.observation_cutoff_ns
        if not 0 <= measurement_age_ns <= policy.image_max_age_ns:
            continue
        selected_left = _latest_arm(left_arm, left_stamps, tick, policy.arm_max_age_ns)
        selected_right = _latest_arm(right_arm, right_stamps, tick, policy.arm_max_age_ns)
        if selected_left is None or selected_right is None:
            continue
        state = make_state(selected_left, selected_right, left_gripper, right_gripper)
        samples.append(
            Pi05Sample(
                sample_index=len(samples),
                tick_ns=tick,
                frame_group_id=group.frame_group_id,
                overview_color=group.overview.color,
                left_color=group.left.color,
                right_color=group.right.color,
                left_arm=selected_left,
                right_arm=selected_right,
                state=state,
                action=state,
            )
        )
    return tuple(samples)


def _runs(mask: list[bool], value: bool) -> list[tuple[int, int]]:
    runs = []
    index = 0
    while index < len(mask):
        if mask[index] is not value:
            index += 1
            continue
        end = index + 1
        while end < len(mask) and mask[end] is value:
            end += 1
        runs.append((index, end))
        index = end
    return runs


def select_nonidle_segments(
    samples: tuple[Pi05Sample, ...],
    policy: Pi05Policy = Pi05Policy(),
) -> tuple[Pi05Segment, ...]:
    if not samples:
        return ()
    idle = [False]
    for previous, current in zip(samples, samples[1:]):
        idle.append(
            all(
                abs(current_value - previous_value) < policy.idle_delta_threshold
                for previous_value, current_value in zip(previous.action, current.action)
            )
        )
    keep = [True] * len(samples)
    for start, end in _runs(idle, True):
        if end - start >= policy.min_idle_frames:
            keep[start:end] = [False] * (end - start)

    segments: list[Pi05Segment] = []
    for start, end in _runs(keep, True):
        if end - start < policy.min_motion_frames:
            continue
        trimmed_end = end - policy.trim_segment_end_frames
        if trimmed_end <= start:
            continue
        segment_samples = samples[start:trimmed_end]
        segments.append(
            Pi05Segment(
                segment_index=len(segments),
                start_sample_index=start,
                end_sample_index=trimmed_end,
                samples=segment_samples,
            )
        )
    return tuple(segments)
