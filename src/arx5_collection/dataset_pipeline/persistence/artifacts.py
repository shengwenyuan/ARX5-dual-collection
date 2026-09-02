from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable
from typing import Mapping
from typing import TypedDict

from arx5_collection.dataset_pipeline.source.models import MessageRef


class MessageRefArtifact(TypedDict):
    topic: str
    sequence: int
    header_stamp_ns: int
    bag_timestamp_ns: int


class ImagePairArtifact(TypedDict):
    stamp_ns: int
    color: MessageRefArtifact
    depth: MessageRefArtifact | None


class ArmRefArtifact(TypedDict):
    ref: MessageRefArtifact
    age_ns: int


class FrameImagesArtifact(TypedDict):
    overview: ImagePairArtifact
    left: ImagePairArtifact
    right: ImagePairArtifact


class FrameArmsArtifact(TypedDict):
    left: ArmRefArtifact
    right: ArmRefArtifact


class FrameGroupArtifact(TypedDict):
    schema_version: int
    episode_id: str
    frame_group_id: int
    observation_cutoff_ns: int
    images: FrameImagesArtifact
    arms: FrameArmsArtifact


class CameraRefsArtifact(TypedDict):
    cam_high: MessageRefArtifact
    cam_left_wrist: MessageRefArtifact
    cam_right_wrist: MessageRefArtifact


class ArmRefsArtifact(TypedDict):
    left: MessageRefArtifact
    right: MessageRefArtifact


class Pi05SampleArtifact(TypedDict):
    schema_version: int
    filter_version: str
    state_action_version: str
    source_episode_id: str
    source_sample_index: int
    tick_ns: int
    frame_group_id: int
    images: CameraRefsArtifact
    arms: ArmRefsArtifact
    state: list[float]
    action: list[float]
    segment_id: str | None
    training_eligible: bool
    exclusion_reason: str | None


class EqualEefSampleArtifact(Pi05SampleArtifact):
    source_header_stamp_ns: int
    delta_time_ns: int
    sampling_reasons: list[str]
    left_eef_delta_m: float
    right_eef_delta_m: float
    left_gripper_delta: float
    right_gripper_delta: float


class Pi05SegmentArtifact(TypedDict):
    schema_version: int
    filter_version: str
    segment_id: str
    source_episode_id: str
    source_start_sample_index: int
    source_end_sample_index_exclusive: int
    start_tick_ns: int
    end_tick_ns: int
    frame_count: int
    task: str


class GripperCalibrationArtifact(TypedDict):
    open_value: float
    closed_value: float
    open_tolerance: float
    closed_tolerance: float


class GripperCalibrationsArtifact(TypedDict):
    contract_id: str
    left: GripperCalibrationArtifact
    right: GripperCalibrationArtifact


class ExcludedEpisodeArtifact(TypedDict):
    episode_id: str
    reason: str


class SelectionReportArtifact(TypedDict):
    schema_version: int
    filter_version: str
    state_action_version: str
    policy: dict[str, object]
    gripper_calibration: GripperCalibrationsArtifact
    source_episode_count: int
    selected_source_episode_count: int
    excluded_episodes: list[ExcludedEpisodeArtifact]
    sample_count: int
    eligible_sample_count: int
    segment_count: int


class EqualEefSamplingContractArtifact(TypedDict):
    mode: str
    eef_field: str
    translation_unit: str
    distance_metric: str
    dual_arm_reduce: str
    eef_distance_m: float
    gripper_delta_threshold: float
    max_sample_interval_ns: int
    timestamp_clock: str
    observation_rule: str
    nominal_fps: int
    horizon_semantics: str


class EqualEefSelectionReportArtifact(SelectionReportArtifact):
    sampling_contract: EqualEefSamplingContractArtifact


def message_ref_to_artifact(ref: MessageRef) -> MessageRefArtifact:
    return {
        "topic": ref.topic,
        "sequence": ref.sequence,
        "header_stamp_ns": ref.header_stamp_ns,
        "bag_timestamp_ns": ref.bag_timestamp_ns,
    }


def message_ref_from_artifact(payload: Mapping[str, object]) -> MessageRef:
    return MessageRef(
        topic=str(payload["topic"]),
        sequence=int(payload["sequence"]),
        header_stamp_ns=int(payload["header_stamp_ns"]),
        bag_timestamp_ns=int(payload["bag_timestamp_ns"]),
    )


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


def read_jsonl(path: Path) -> list[dict[str, object]]:
    rows = []
    with path.open() as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"expected a JSON object at {path}:{line_number}")
            rows.append(row)
    return rows


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    with path.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
