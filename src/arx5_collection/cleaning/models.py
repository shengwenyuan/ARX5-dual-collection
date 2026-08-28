from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arx5_collection.capture import CaptureProfile
from arx5_collection.capture import stream_contract


CAMERA_ROLES = ("left", "right", "overview")
CAMERA_LEAVES = ("color", "aligned_depth")


def camera_topic(role: str, leaf: str) -> str:
    suffix = "color/image_raw" if leaf == "color" else "aligned_depth/image_raw"
    return f"/sensors/camera_{role}/{suffix}"


LEFT_ARM_TOPIC = "/embodiments/left_arm/state"
RIGHT_ARM_TOPIC = "/embodiments/right_arm/state"
REQUIRED_TOPICS = tuple(stream_contract(CaptureProfile.RGBD).values())


def required_topics(profile: CaptureProfile) -> tuple[str, ...]:
    return tuple(stream_contract(profile).values())


@dataclass(frozen=True, slots=True)
class MessageRef:
    topic: str
    sequence: int
    header_stamp_ns: int
    bag_timestamp_ns: int

    def __post_init__(self) -> None:
        if not self.topic:
            raise ValueError("topic must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if self.header_stamp_ns < 0 or self.bag_timestamp_ns < 0:
            raise ValueError("timestamps must not be negative")

@dataclass(frozen=True, slots=True)
class ArmSample:
    ref: MessageRef
    joint_positions: tuple[float, ...]
    gripper_position: float
    eef_xyzrpy: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if len(self.joint_positions) != 6:
            raise ValueError("joint_positions must contain six values")
        if len(self.eef_xyzrpy) != 6:
            raise ValueError("eef_xyzrpy must contain six values")


@dataclass(frozen=True, slots=True)
class ImagePair:
    color: MessageRef
    depth: MessageRef | None

    def __post_init__(self) -> None:
        if (
            self.depth is not None
            and self.color.header_stamp_ns != self.depth.header_stamp_ns
        ):
            raise ValueError("paired color and depth timestamps must match")

    @property
    def stamp_ns(self) -> int:
        return self.color.header_stamp_ns

@dataclass(frozen=True, slots=True)
class FrameGroup:
    frame_group_id: int
    overview: ImagePair
    left: ImagePair
    right: ImagePair
    observation_cutoff_ns: int
    left_arm: ArmSample
    right_arm: ArmSample

    def __post_init__(self) -> None:
        if self.frame_group_id < 0:
            raise ValueError("frame_group_id must not be negative")
        latest_image = max(self.overview.stamp_ns, self.left.stamp_ns, self.right.stamp_ns)
        if self.observation_cutoff_ns != latest_image:
            raise ValueError("observation_cutoff_ns must be the latest selected image timestamp")
        if self.left_arm.ref.header_stamp_ns > self.observation_cutoff_ns:
            raise ValueError("left arm sample must not be newer than the observation")
        if self.right_arm.ref.header_stamp_ns > self.observation_cutoff_ns:
            raise ValueError("right arm sample must not be newer than the observation")


@dataclass(frozen=True, slots=True)
class EpisodeScan:
    episode_dir: Path
    refs_by_topic: dict[str, tuple[MessageRef, ...]]
    left_arm: tuple[ArmSample, ...]
    right_arm: tuple[ArmSample, ...]
    topic_types: dict[str, str]
    capture_profile: CaptureProfile = CaptureProfile.RGBD

    @property
    def mcap_path(self) -> Path:
        return self.episode_dir / "episode.mcap"


@dataclass(frozen=True, slots=True)
class CleaningPolicy:
    cross_camera_tolerance_ns: int = 16_700_000
    arm_max_age_ns: int = 2_000_000
    camera_gap_warning_ns: int = 67_000_000
    arm_gap_warning_ns: int = 3_000_000
    grade_a_coverage: float = 0.99
    grade_b_coverage: float = 0.95

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.cross_camera_tolerance_ns,
                self.arm_max_age_ns,
                self.camera_gap_warning_ns,
                self.arm_gap_warning_ns,
            )
        ):
            raise ValueError("timestamp tolerances must not be negative")
        if not 0 <= self.grade_b_coverage <= self.grade_a_coverage <= 1:
            raise ValueError("grade coverage thresholds must satisfy 0 <= B <= A <= 1")
