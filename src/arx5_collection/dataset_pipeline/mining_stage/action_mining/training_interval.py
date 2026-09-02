from __future__ import annotations

from dataclasses import replace

from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    SegmentProvenance,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.utils import (
    load_frame_groups,
)

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import ActionMiningInterval
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import exclude_episode
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    metadata = require_output(context.metadata, "metadata_check")
    scan = require_output(context.scan, "mcap_check")
    cleaning = require_output(context.cleaning, "alignment_report")
    if cleaning.output_dir is None:
        raise RuntimeError("alignment_report did not persist artifacts")

    def operation() -> tuple[ActionMiningInterval, ...]:
        groups = load_frame_groups(cleaning.output_dir / "frame_index.jsonl", scan)
        max_duration_s = float(unit.params["max_episode_duration_s"])
        if metadata.get("collection_type", "demonstration") != "dagger":
            if (
                groups
                and (groups[-1].observation_cutoff_ns - groups[0].observation_cutoff_ns)
                / 1e9
                > max_duration_s
            ):
                return ()
            return (ActionMiningInterval(scan, groups, None),)
        authority = require_output(context.authority, "dagger_authority")
        intervals = []
        for correction in authority.expert_segments:
            start_ns = correction.started_bag_timestamp_ns
            end_ns = correction.ended_bag_timestamp_ns
            correction_scan = _slice_scan(scan, start_ns, end_ns)
            correction_groups = _slice_groups(groups, start_ns, end_ns)
            if (
                correction_groups
                and (
                    correction_groups[-1].observation_cutoff_ns
                    - correction_groups[0].observation_cutoff_ns
                )
                / 1e9
                > max_duration_s
            ):
                continue
            intervals.append(
                ActionMiningInterval(
                    correction_scan,
                    correction_groups,
                    SegmentProvenance(
                        collection_type="dagger",
                        training_class="expert_correction",
                        intervention_id=correction.intervention_id,
                        authority_segment_id=correction.segment_id,
                        source_started_bag_timestamp_ns=start_ns,
                        source_ended_bag_timestamp_ns=end_ns,
                    ),
                )
            )
        return tuple(intervals)

    context.mining_intervals = timed(unit.type, operation)
    if (
        metadata.get("collection_type", "demonstration") != "dagger"
        and not context.mining_intervals
    ):
        exclude_episode(context, "episode_too_long")


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
