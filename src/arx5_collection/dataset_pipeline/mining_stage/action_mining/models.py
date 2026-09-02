from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from arx5_collection.dataset_pipeline.persistence.artifacts import (
    ExcludedEpisodeArtifact,
)
from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import MessageRef
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.utils import (
    ACTION_HORIZON,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.utils import (
    DATASET_FPS,
)
from arx5_collection.collection.dagger.topics import AUTHORITY_TOPIC
from arx5_collection.collection.dagger.topics import AUTHORITY_TYPE


class AuthorityEventType(IntEnum):
    TAKEOVER_REQUESTED = 1
    HUMAN_ACTIVE = 2
    RESUME_REQUESTED = 3
    POLICY_ACTIVE = 4
    FAULT_HOLD = 5


class AuthorityClass(str, Enum):
    POLICY = "policy"
    HANDOVER = "handover"
    EXPERT_CORRECTION = "expert_correction"
    RESUME = "resume"
    FAULT = "fault"


@dataclass(frozen=True, slots=True)
class AuthorityEventRecord:
    sequence: int
    monotonic_time_ns: int
    intervention_id: int
    control_epoch: int
    event_type: AuthorityEventType
    reason: str
    bag_timestamp_ns: int
    header_stamp_ns: int

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("authority sequence must be positive")
        if (
            min(
                self.monotonic_time_ns,
                self.intervention_id,
                self.control_epoch,
                self.bag_timestamp_ns,
                self.header_stamp_ns,
            )
            < 0
        ):
            raise ValueError("authority values must not be negative")


@dataclass(frozen=True, slots=True)
class AuthoritySegment:
    segment_id: str
    authority_class: AuthorityClass
    started_offset_ns: int
    ended_offset_ns: int
    started_bag_timestamp_ns: int
    ended_bag_timestamp_ns: int
    intervention_id: int | None
    complete: bool
    training_eligible: bool
    exclusion_reason: str | None

    def __post_init__(self) -> None:
        if self.started_offset_ns < 0 or self.ended_offset_ns < self.started_offset_ns:
            raise ValueError(
                "authority segment offsets must be ordered and non-negative"
            )
        if self.ended_bag_timestamp_ns < self.started_bag_timestamp_ns:
            raise ValueError("authority segment bag timestamps must be ordered")
        if self.authority_class is AuthorityClass.EXPERT_CORRECTION:
            if self.intervention_id is None or self.intervention_id <= 0:
                raise ValueError("expert correction requires an intervention id")
        if self.training_eligible != (
            self.authority_class is AuthorityClass.EXPERT_CORRECTION and self.complete
        ):
            raise ValueError(
                "only complete expert corrections may be training eligible"
            )


@dataclass(frozen=True, slots=True)
class AuthorityClassification:
    episode_id: str
    valid: bool
    issues: tuple[str, ...]
    episode_monotonic_anchor_ns: int | None
    episode_bag_anchor_ns: int | None
    bag_anchor_spread_ns: int | None
    event_count: int
    intervention_count: int
    segments: tuple[AuthoritySegment, ...]

    @property
    def expert_segments(self) -> tuple[AuthoritySegment, ...]:
        return tuple(segment for segment in self.segments if segment.training_eligible)


@dataclass(frozen=True, slots=True)
class Pi05Policy:
    fps: int = DATASET_FPS
    action_horizon: int = ACTION_HORIZON
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


class SegmentPolicy(Protocol):
    idle_delta_threshold: float
    min_idle_frames: int
    min_motion_frames: int
    trim_segment_end_frames: int


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


@dataclass(frozen=True, slots=True)
class SegmentProvenance:
    collection_type: str
    training_class: str
    intervention_id: int | None = None
    authority_segment_id: str | None = None
    source_started_bag_timestamp_ns: int | None = None
    source_ended_bag_timestamp_ns: int | None = None

    def __post_init__(self) -> None:
        if self.collection_type not in {"demonstration", "dagger"}:
            raise ValueError("invalid segment collection type")
        if not self.training_class:
            raise ValueError("training class must not be empty")
        if self.collection_type == "dagger":
            if self.training_class != "expert_correction":
                raise ValueError("DAgger selection only accepts expert corrections")
            if self.intervention_id is None or self.intervention_id <= 0:
                raise ValueError("DAgger provenance requires intervention_id")
            if not self.authority_segment_id:
                raise ValueError("DAgger provenance requires authority_segment_id")
            if (
                self.source_started_bag_timestamp_ns is None
                or self.source_ended_bag_timestamp_ns is None
                or self.source_ended_bag_timestamp_ns
                < self.source_started_bag_timestamp_ns
            ):
                raise ValueError("DAgger provenance requires ordered bag boundaries")


@dataclass(frozen=True, slots=True)
class EpisodeSelection:
    episode_id: str
    task: str
    samples: tuple[Pi05Sample, ...]
    segments: tuple[Pi05Segment, ...]
    segment_provenance: tuple[SegmentProvenance, ...] = ()
    source_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("task must not be empty")
        if self.source_session_id is not None and not self.source_session_id:
            raise ValueError("source Session id must not be empty")
        if self.segment_provenance and len(self.segment_provenance) != len(
            self.segments
        ):
            raise ValueError("segment provenance must match selected segments")


@dataclass(frozen=True, slots=True)
class DatasetSelection:
    episodes: tuple[EpisodeSelection, ...]
    excluded_episodes: tuple[ExcludedEpisodeArtifact, ...]
    output_dir: Path | None = None
