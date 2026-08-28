from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable
from typing import Mapping
from typing import TYPE_CHECKING
from typing import cast

from arx5_collection.artifacts import ArmRefsArtifact
from arx5_collection.artifacts import CameraRefsArtifact
from arx5_collection.artifacts import ExcludedEpisodeArtifact
from arx5_collection.artifacts import EqualEefSampleArtifact
from arx5_collection.artifacts import EqualEefSamplingContractArtifact
from arx5_collection.artifacts import GripperCalibrationArtifact
from arx5_collection.artifacts import GripperCalibrationsArtifact
from arx5_collection.artifacts import Pi05SampleArtifact
from arx5_collection.artifacts import Pi05SegmentArtifact
from arx5_collection.artifacts import SelectionReportArtifact
from arx5_collection.artifacts import message_ref_from_artifact
from arx5_collection.artifacts import message_ref_to_artifact
from arx5_collection.artifacts import read_jsonl
from arx5_collection.artifacts import write_json
from arx5_collection.artifacts import write_jsonl
from arx5_collection.atomic import staged_directory
from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.cleaning.models import ImagePair
from arx5_collection.cleaning.models import LEFT_ARM_TOPIC
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.cleaning.models import RIGHT_ARM_TOPIC
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.gripper import ARX5_GRIPPER_CONTRACT_ID
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy
from arx5_collection.pi05_dataset.eef_selection import EqualEefSample
from arx5_collection.pi05_dataset.selection import Pi05Policy
from arx5_collection.pi05_dataset.selection import Pi05Sample
from arx5_collection.pi05_dataset.selection import Pi05Segment
from arx5_collection.pi05_dataset.provenance import SegmentProvenance
from arx5_collection.pi05_dataset.provenance import provenance_row

if TYPE_CHECKING:
    from arx5_collection.pi05_dataset.selection_pipeline import DatasetSelection
    from arx5_collection.pi05_dataset.selection_pipeline import EpisodeSelection


FILTER_VERSION = "pi05-arx-filter-v1"
EQUAL_EEF_FILTER_VERSION = "pi05-arx-filter-v2-equal-eef-distance"
STATE_ACTION_VERSION = "arx5-measured-position-proxy-v1"
SCHEMA_VERSION = 1
EQUAL_EEF_SCHEMA_VERSION = 2


SampleEncoder = Callable[[str, Pi05Sample, str | None], Pi05SampleArtifact]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _ref_lookup(scan: EpisodeScan) -> dict[tuple[str, int], MessageRef]:
    return {
        (ref.topic, ref.sequence): ref
        for refs in scan.refs_by_topic.values()
        for ref in refs
    }


def _arm_lookup(samples: tuple[ArmSample, ...]) -> dict[tuple[str, int], ArmSample]:
    return {(sample.ref.topic, sample.ref.sequence): sample for sample in samples}


def _checked_ref(payload: Mapping[str, object], refs: dict[tuple[str, int], MessageRef]) -> MessageRef:
    expected = message_ref_from_artifact(payload)
    key = (expected.topic, expected.sequence)
    try:
        actual = refs[key]
    except KeyError as error:
        raise ValueError(f"frame index references a missing MCAP message: {key}") from error
    if actual != expected:
        raise ValueError(f"frame index reference changed for {key}: expected={expected}, actual={actual}")
    return actual


def load_frame_groups(frame_index_path: Path, scan: EpisodeScan) -> tuple[FrameGroup, ...]:
    refs = _ref_lookup(scan)
    arms = {**_arm_lookup(scan.left_arm), **_arm_lookup(scan.right_arm)}
    groups = []
    for row in read_jsonl(frame_index_path):
        images = _mapping(row["images"], "images")
        image_pairs = {}
        for role in ("overview", "left", "right"):
            pair = _mapping(images[role], f"images.{role}")
            depth = pair["depth"]
            image_pairs[role] = ImagePair(
                color=_checked_ref(_mapping(pair["color"], f"images.{role}.color"), refs),
                depth=(
                    None
                    if depth is None
                    else _checked_ref(
                        _mapping(depth, f"images.{role}.depth"), refs
                    )
                ),
            )
        arm_refs = _mapping(row["arms"], "arms")
        arm_samples = {}
        for side, expected_topic in (("left", LEFT_ARM_TOPIC), ("right", RIGHT_ARM_TOPIC)):
            arm = _mapping(arm_refs[side], f"arms.{side}")
            ref = _checked_ref(_mapping(arm["ref"], f"arms.{side}.ref"), refs)
            if ref.topic != expected_topic:
                raise ValueError(f"{side} arm frame index references {ref.topic}")
            try:
                arm_samples[side] = arms[(ref.topic, ref.sequence)]
            except KeyError as error:
                raise ValueError(
                    f"frame index references a discarded non-finite arm sample: {ref}"
                ) from error
        groups.append(
            FrameGroup(
                frame_group_id=int(row["frame_group_id"]),
                overview=image_pairs["overview"],
                left=image_pairs["left"],
                right=image_pairs["right"],
                observation_cutoff_ns=int(row["observation_cutoff_ns"]),
                left_arm=arm_samples["left"],
                right_arm=arm_samples["right"],
            )
        )
    return tuple(groups)


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
        exclusion_reason=None if segment_id is not None else "idle_or_short_motion_range",
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


def _calibration_artifact(calibration: GripperCalibration) -> GripperCalibrationArtifact:
    return GripperCalibrationArtifact(**asdict(calibration))


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
    session_tasks: dict[str, set[str]] = {}
    for episode in selection.episodes:
        session_id = episode.source_session_id or episode.episode_id
        session_tasks.setdefault(session_id, set()).add(episode.task)
    for session_id, tasks in session_tasks.items():
        if len(tasks) != 1:
            raise ValueError(f"task mismatch within source Session: {session_id}")

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
                memberships[(episode.episode_id, sample.sample_index)] = row["segment_id"]
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
