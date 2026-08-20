from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from enum import IntEnum


AUTHORITY_TOPIC = "/dagger/authority"
AUTHORITY_TYPE = "arx5_collection_interfaces/msg/AuthorityEvent"


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
        if min(
            self.monotonic_time_ns,
            self.intervention_id,
            self.control_epoch,
            self.bag_timestamp_ns,
            self.header_stamp_ns,
        ) < 0:
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
            raise ValueError("authority segment offsets must be ordered and non-negative")
        if self.ended_bag_timestamp_ns < self.started_bag_timestamp_ns:
            raise ValueError("authority segment bag timestamps must be ordered")
        if self.authority_class is AuthorityClass.EXPERT_CORRECTION:
            if self.intervention_id is None or self.intervention_id <= 0:
                raise ValueError("expert correction requires an intervention id")
        if self.training_eligible != (
            self.authority_class is AuthorityClass.EXPERT_CORRECTION and self.complete
        ):
            raise ValueError("only complete expert corrections may be training eligible")


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
