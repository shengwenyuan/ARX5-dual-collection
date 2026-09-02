from __future__ import annotations

from bisect import bisect_right
from dataclasses import replace

from arx5_collection.common.gripper import GripperCalibration
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    Pi05Policy,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    Pi05Sample,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    Pi05Segment,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    SegmentPolicy,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.utils import make_state
from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    SegmentProvenance,
)
from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import exclude_episode
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
        selected_right = _latest_arm(
            right_arm, right_stamps, tick, policy.arm_max_age_ns
        )
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
    policy: SegmentPolicy = Pi05Policy(),
) -> tuple[Pi05Segment, ...]:
    if not samples:
        return ()
    idle = [False]
    for previous, current in zip(samples, samples[1:]):
        idle.append(
            all(
                abs(current_value - previous_value) < policy.idle_delta_threshold
                for previous_value, current_value in zip(
                    previous.action, current.action
                )
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


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    metadata = require_output(context.metadata, "metadata_check")
    intervals = require_output(context.mining_intervals, "training_interval")
    interval_samples = require_output(
        context.interval_samples,
        "equal_eef_action_sampler",
    )

    def operation() -> tuple[
        tuple[Pi05Sample, ...],
        tuple[Pi05Segment, ...],
        tuple[SegmentProvenance, ...],
    ]:
        samples: list[Pi05Sample] = []
        segments: list[Pi05Segment] = []
        provenance = []
        for interval, local_samples in zip(intervals, interval_samples, strict=True):
            local_segments = select_nonidle_segments(
                local_samples,
                context.recipe.selection,
            )
            renumbered_samples, renumbered_segments = _renumber(
                local_samples,
                local_segments,
                len(samples),
                len(segments),
            )
            samples.extend(renumbered_samples)
            segments.extend(renumbered_segments)
            if interval.provenance is not None:
                provenance.extend(interval.provenance for _ in renumbered_segments)
        return tuple(samples), tuple(segments), tuple(provenance)

    (
        context.mined_samples,
        context.mined_segments,
        context.segment_provenance,
    ) = timed(unit.type, operation)
    if not context.mined_segments:
        reason = (
            "no_valid_correction_motion_segment"
            if metadata.get("collection_type", "demonstration") == "dagger"
            else "no_valid_motion_segment"
        )
        exclude_episode(context, reason)


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
        for original, current in zip(samples, replaced, strict=True)
    }
    new_segments = tuple(
        Pi05Segment(
            segment_index=segment_offset + index,
            start_sample_index=by_local_index[
                segment.samples[0].sample_index
            ].sample_index,
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
