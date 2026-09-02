from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CollectionType(str, Enum):
    DEMONSTRATION = "demonstration"
    DAGGER = "dagger"


class ControlOwner(str, Enum):
    MODEL = "model"
    HUMAN = "human"


class ShadowQuality(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ShadowMetadata:
    quality: ShadowQuality
    inference_attempt_count: int
    inference_success_count: int
    inference_failure_count: int
    recovery_count: int

    def __post_init__(self) -> None:
        values = (
            self.inference_attempt_count,
            self.inference_success_count,
            self.inference_failure_count,
            self.recovery_count,
        )
        if any(value < 0 for value in values):
            raise ValueError("Shadow counters must not be negative")
        if self.inference_attempt_count != (
            self.inference_success_count + self.inference_failure_count
        ):
            raise ValueError("Shadow attempts must equal successes plus failures")
        if self.recovery_count > self.inference_success_count:
            raise ValueError("Shadow recoveries must not exceed successes")

    def to_dict(self) -> dict[str, object]:
        return {
            "quality": self.quality.value,
            "inference_attempt_count": self.inference_attempt_count,
            "inference_success_count": self.inference_success_count,
            "inference_failure_count": self.inference_failure_count,
            "recovery_count": self.recovery_count,
        }


@dataclass(frozen=True, slots=True)
class ControlSegment:
    owner: ControlOwner
    started_offset_s: float
    ended_offset_s: float
    intervention_id: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.started_offset_s) or self.started_offset_s < 0:
            raise ValueError("control segment start must be finite and non-negative")
        if not math.isfinite(self.ended_offset_s):
            raise ValueError("control segment end must be finite")
        if self.ended_offset_s < self.started_offset_s:
            raise ValueError("control segment end must not precede its start")
        if self.intervention_id is not None and self.intervention_id <= 0:
            raise ValueError("intervention_id must be positive")
        if self.owner is ControlOwner.HUMAN and self.intervention_id is None:
            raise ValueError("human control segment requires intervention_id")
        if self.owner is ControlOwner.MODEL and self.intervention_id is not None:
            raise ValueError("model control segment must not have intervention_id")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "owner": self.owner.value,
            "started_offset_s": self.started_offset_s,
            "ended_offset_s": self.ended_offset_s,
        }
        if self.intervention_id is not None:
            value["intervention_id"] = self.intervention_id
        return value


@dataclass(frozen=True, slots=True)
class DaggerMetadata:
    checkpoint_sha256: str
    intervention_count: int
    control_segments: tuple[ControlSegment, ...]
    shadow: ShadowMetadata | None = None

    def __post_init__(self) -> None:
        normalized = self.checkpoint_sha256.lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "checkpoint_sha256", normalized)
        if self.intervention_count < 0:
            raise ValueError("intervention_count must not be negative")

        previous_end = 0.0
        intervention_ids = set()
        for segment in self.control_segments:
            if segment.started_offset_s < previous_end:
                raise ValueError("control segments must be ordered and non-overlapping")
            previous_end = segment.ended_offset_s
            if segment.intervention_id is not None:
                intervention_ids.add(segment.intervention_id)
        if any(value > self.intervention_count for value in intervention_ids):
            raise ValueError(
                "control segment intervention_id exceeds intervention_count"
            )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "checkpoint_sha256": self.checkpoint_sha256,
            "intervention_count": self.intervention_count,
            "control_segments": [
                segment.to_dict() for segment in self.control_segments
            ],
        }
        if self.shadow is not None:
            value["shadow"] = self.shadow.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class MetadataContext:
    collection_type: CollectionType
    dagger: DaggerMetadata | None = None

    def __post_init__(self) -> None:
        if self.collection_type is CollectionType.DAGGER and self.dagger is None:
            raise ValueError("dagger collection requires DaggerMetadata")
        if (
            self.collection_type is not CollectionType.DAGGER
            and self.dagger is not None
        ):
            raise ValueError("DaggerMetadata is only valid for dagger collection")

    @classmethod
    def demonstration(cls) -> MetadataContext:
        return cls(CollectionType.DEMONSTRATION)

    @classmethod
    def for_dagger(cls, dagger: DaggerMetadata) -> MetadataContext:
        return cls(CollectionType.DAGGER, dagger)
