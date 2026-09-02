from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from arx5_collection.common.gripper import GripperCalibration
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    message_ref_from_artifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import read_jsonl
from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.source.models import ImagePair
from arx5_collection.dataset_pipeline.source.models import LEFT_ARM_TOPIC
from arx5_collection.dataset_pipeline.source.models import MessageRef
from arx5_collection.dataset_pipeline.source.models import RIGHT_ARM_TOPIC


def make_state(
    left: ArmSample,
    right: ArmSample,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
) -> tuple[float, ...]:
    return (
        *left.joint_positions,
        left_gripper.normalize(left.gripper_position),
        *right.joint_positions,
        right_gripper.normalize(right.gripper_position),
    )


def derive_source_session_id(episode_dir: Path) -> str:
    metadata = json.loads((episode_dir / "metadata.json").read_text())
    station = metadata.get("station", {})
    timing = metadata.get("timing", {})
    station_id = station.get("id") if isinstance(station, dict) else None
    started_at = timing.get("started_at") if isinstance(timing, dict) else None
    day = (
        started_at[:10]
        if isinstance(started_at, str) and len(started_at) >= 10
        else None
    )
    parts = [station_id, day, episode_dir.parent.name]
    return "/".join(str(part) for part in parts if part)


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


def _checked_ref(
    payload: Mapping[str, object],
    refs: dict[tuple[str, int], MessageRef],
) -> MessageRef:
    expected = message_ref_from_artifact(payload)
    key = (expected.topic, expected.sequence)
    try:
        actual = refs[key]
    except KeyError as error:
        raise ValueError(
            f"frame index references a missing MCAP message: {key}"
        ) from error
    if actual != expected:
        raise ValueError(
            f"frame index reference changed for {key}: expected={expected}, actual={actual}"
        )
    return actual


def load_frame_groups(
    frame_index_path: Path,
    scan: EpisodeScan,
) -> tuple[FrameGroup, ...]:
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
                color=_checked_ref(
                    _mapping(pair["color"], f"images.{role}.color"), refs
                ),
                depth=(
                    None
                    if depth is None
                    else _checked_ref(_mapping(depth, f"images.{role}.depth"), refs)
                ),
            )
        arm_refs = _mapping(row["arms"], "arms")
        arm_samples = {}
        for side, expected_topic in (
            ("left", LEFT_ARM_TOPIC),
            ("right", RIGHT_ARM_TOPIC),
        ):
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
