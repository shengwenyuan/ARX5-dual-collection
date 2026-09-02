from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable
from typing import cast

from arx5_collection.common.gripper import ARX5_GRIPPER_CONTRACT_ID
from arx5_collection.common.gripper import GripperCalibration
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    DatasetSelection,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    EqualEefPolicy,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    EqualEefSample,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    EpisodeSelection,
)
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
    SegmentProvenance,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import ArmRefsArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import CameraRefsArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    ExcludedEpisodeArtifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    EqualEefSampleArtifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    EqualEefSamplingContractArtifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    GripperCalibrationArtifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    GripperCalibrationsArtifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import Pi05SampleArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import Pi05SegmentArtifact
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    SelectionReportArtifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    message_ref_to_artifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import write_json
from arx5_collection.dataset_pipeline.persistence.artifacts import write_jsonl
from arx5_collection.dataset_pipeline.persistence.atomic import staged_directory
from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


FILTER_VERSION = "pi05-arx-filter-v1"
EQUAL_EEF_FILTER_VERSION = "pi05-arx-filter-v2-equal-eef-distance"
STATE_ACTION_VERSION = "arx5-measured-position-proxy-v1"
SCHEMA_VERSION = 1
EQUAL_EEF_SCHEMA_VERSION = 2


SampleEncoder = Callable[[str, Pi05Sample, str | None], Pi05SampleArtifact]


def sample_to_artifact(
    episode_id: str,
    sample: Pi05Sample,
    segment_id: str | None,
) -> Pi05SampleArtifact:
    images = CameraRefsArtifact(
        cam_high=message_ref_to_artifact(sample.overview_color),
        cam_left_wrist=message_ref_to_artifact(sample.left_color),
        cam_right_wrist=message_ref_to_artifact(sample.right_color),
    )
    arms = ArmRefsArtifact(
        left=message_ref_to_artifact(sample.left_arm.ref),
        right=message_ref_to_artifact(sample.right_arm.ref),
    )
    return Pi05SampleArtifact(
        schema_version=SCHEMA_VERSION,
        filter_version=FILTER_VERSION,
        state_action_version=STATE_ACTION_VERSION,
        source_episode_id=episode_id,
        source_sample_index=sample.sample_index,
        tick_ns=sample.tick_ns,
        frame_group_id=sample.frame_group_id,
        images=images,
        arms=arms,
        state=list(sample.state),
        action=list(sample.action),
        segment_id=segment_id,
        training_eligible=segment_id is not None,
        exclusion_reason=(
            None if segment_id is not None else "idle_or_short_motion_range"
        ),
    )


def equal_eef_sample_to_artifact(
    episode_id: str,
    sample: Pi05Sample,
    segment_id: str | None,
) -> EqualEefSampleArtifact:
    if not isinstance(sample, EqualEefSample):
        raise TypeError("equal EEF artifacts require EqualEefSample")
    artifact = cast(
        EqualEefSampleArtifact,
        sample_to_artifact(episode_id, sample, segment_id),
    )
    artifact.update(
        schema_version=EQUAL_EEF_SCHEMA_VERSION,
        filter_version=EQUAL_EEF_FILTER_VERSION,
        source_header_stamp_ns=sample.tick_ns,
        delta_time_ns=sample.delta_time_ns,
        sampling_reasons=list(sample.sampling_reasons),
        left_eef_delta_m=sample.left_eef_delta_m,
        right_eef_delta_m=sample.right_eef_delta_m,
        left_gripper_delta=sample.left_gripper_delta,
        right_gripper_delta=sample.right_gripper_delta,
    )
    return artifact


def segment_to_artifact(
    episode: EpisodeSelection,
    segment: Pi05Segment,
    *,
    schema_version: int = SCHEMA_VERSION,
    filter_version: str = FILTER_VERSION,
) -> Pi05SegmentArtifact:
    first = segment.samples[0]
    last = segment.samples[-1]
    return Pi05SegmentArtifact(
        schema_version=schema_version,
        filter_version=filter_version,
        segment_id=f"{episode.episode_id}--{segment.segment_index:03d}",
        source_episode_id=episode.episode_id,
        source_start_sample_index=segment.start_sample_index,
        source_end_sample_index_exclusive=segment.end_sample_index,
        start_tick_ns=first.tick_ns,
        end_tick_ns=last.tick_ns,
        frame_count=len(segment.samples),
        task=episode.task,
    )


def _calibration_artifact(
    calibration: GripperCalibration,
) -> GripperCalibrationArtifact:
    return GripperCalibrationArtifact(**asdict(calibration))


def provenance_row(
    segment_id: str,
    source_episode_id: str,
    source_session_id: str,
    provenance: SegmentProvenance,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "segment_id": segment_id,
        "source_episode_id": source_episode_id,
        "source_session_id": source_session_id,
        "split_group": source_episode_id,
        "collection_type": provenance.collection_type,
        "training_class": provenance.training_class,
        "intervention_id": provenance.intervention_id,
        "authority_segment_id": provenance.authority_segment_id,
        "source_started_bag_timestamp_ns": provenance.source_started_bag_timestamp_ns,
        "source_ended_bag_timestamp_ns": provenance.source_ended_bag_timestamp_ns,
        "sample_weight": 1.0,
    }


def _write_selection_artifacts(
    output_root: Path,
    selection: DatasetSelection,
    policy: Pi05Policy | EqualEefPolicy,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
    *,
    schema_version: int,
    filter_version: str,
    sample_encoder: SampleEncoder,
    sampling_contract: EqualEefSamplingContractArtifact | None = None,
) -> Path:
    target = output_root / "selection"
    segment_rows = []
    source_rows = []
    memberships: dict[tuple[str, int], str] = {}
    for episode in selection.episodes:
        provenance_items = episode.segment_provenance or tuple(
            SegmentProvenance("demonstration", "demonstration")
            for _ in episode.segments
        )
        for segment, provenance in zip(episode.segments, provenance_items):
            row = segment_to_artifact(
                episode,
                segment,
                schema_version=schema_version,
                filter_version=filter_version,
            )
            segment_rows.append(row)
            source_rows.append(
                provenance_row(
                    row["segment_id"],
                    episode.episode_id,
                    episode.source_session_id or episode.episode_id,
                    provenance,
                )
            )
            for sample in segment.samples:
                memberships[(episode.episode_id, sample.sample_index)] = row[
                    "segment_id"
                ]
    sample_rows = [
        sample_encoder(
            episode.episode_id,
            sample,
            memberships.get((episode.episode_id, sample.sample_index)),
        )
        for episode in selection.episodes
        for sample in episode.samples
    ]
    calibrations = GripperCalibrationsArtifact(
        contract_id=ARX5_GRIPPER_CONTRACT_ID,
        left=_calibration_artifact(left_gripper),
        right=_calibration_artifact(right_gripper),
    )
    excluded = cast(list[ExcludedEpisodeArtifact], list(selection.excluded_episodes))
    report: dict[str, object] = SelectionReportArtifact(
        schema_version=schema_version,
        filter_version=filter_version,
        state_action_version=STATE_ACTION_VERSION,
        policy=cast(dict[str, object], asdict(policy)),
        gripper_calibration=calibrations,
        source_episode_count=len(selection.episodes) + len(selection.excluded_episodes),
        selected_source_episode_count=len(selection.episodes),
        excluded_episodes=excluded,
        sample_count=len(sample_rows),
        eligible_sample_count=sum(row["training_eligible"] for row in sample_rows),
        segment_count=len(segment_rows),
    )
    report["source_composition"] = {
        key: sum(row["collection_type"] == key for row in source_rows)
        for key in ("demonstration", "dagger")
    }
    if sampling_contract is not None:
        report["sampling_contract"] = sampling_contract
    with staged_directory(target) as temporary:
        write_jsonl(temporary / "sample_index.jsonl", sample_rows)
        write_jsonl(temporary / "segments.jsonl", segment_rows)
        write_jsonl(temporary / "source_manifest.jsonl", source_rows)
        write_json(temporary / "selection.json", report)
    return target


def write_selection_artifacts(
    output_root: Path,
    selection: DatasetSelection,
    policy: Pi05Policy,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
) -> Path:
    return _write_selection_artifacts(
        output_root,
        selection,
        policy,
        left_gripper,
        right_gripper,
        schema_version=SCHEMA_VERSION,
        filter_version=FILTER_VERSION,
        sample_encoder=sample_to_artifact,
    )


def write_equal_eef_selection_artifacts(
    output_root: Path,
    selection: DatasetSelection,
    policy: EqualEefPolicy,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
) -> Path:
    sampling_contract = EqualEefSamplingContractArtifact(
        mode="equal_eef_distance",
        eef_field="ArmState.eef_xyzrpy[:3]",
        translation_unit="metre",
        distance_metric="endpoint_euclidean",
        dual_arm_reduce="max",
        eef_distance_m=policy.eef_distance_m,
        gripper_delta_threshold=policy.gripper_delta_threshold,
        max_sample_interval_ns=policy.max_sample_interval_ns,
        timestamp_clock="header_stamp_ns",
        observation_rule="latest_complete_frame_group_at_or_before_tick",
        nominal_fps=policy.nominal_fps,
        horizon_semantics="trajectory_steps",
    )
    return _write_selection_artifacts(
        output_root,
        selection,
        policy,
        left_gripper,
        right_gripper,
        schema_version=EQUAL_EEF_SCHEMA_VERSION,
        filter_version=EQUAL_EEF_FILTER_VERSION,
        sample_encoder=equal_eef_sample_to_artifact,
        sampling_contract=sampling_contract,
    )


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    samples = require_output(context.mined_samples, "motion_segmenter")
    segments = require_output(context.mined_segments, "motion_segmenter")

    def operation() -> DatasetSelection:
        episode = EpisodeSelection(
            episode_id=context.receipt.episode_id,
            task=context.task,
            samples=samples,
            segments=segments,
            segment_provenance=context.segment_provenance,
            source_session_id=context.receipt.source_session_id,
        )
        result = DatasetSelection((episode,), ())
        output_dir = write_equal_eef_selection_artifacts(
            context.output_root,
            result,
            context.recipe.selection,
            context.recipe.gripper,
            context.recipe.gripper,
        )
        return DatasetSelection(result.episodes, result.excluded_episodes, output_dir)

    context.selection = timed(unit.type, operation)
