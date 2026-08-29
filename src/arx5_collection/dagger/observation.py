from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from arx5_collection.gripper import GripperCalibration


PI05_IMAGE_WIDTH = 640
PI05_IMAGE_HEIGHT = 360
PI05_IMAGE_CHANNELS = 3


class ObservationFailureCode(str, Enum):
    BUFFERS_NOT_READY = "buffers_not_ready"
    CAMERA_SPAN_EXCEEDED = "camera_span_exceeded"
    SNAPSHOT_STALE = "snapshot_stale"
    LEFT_ARM_STALE = "left_arm_stale"
    RIGHT_ARM_STALE = "right_arm_stale"


class ObservationUnavailableError(RuntimeError):
    """No real causal step currently satisfies the timing contract."""

    def __init__(
        self,
        code: ObservationFailureCode,
        *,
        observed_ns: int | None = None,
        limit_ns: int | None = None,
        detail: str = "",
    ) -> None:
        self.code = code
        self.observed_ns = observed_ns
        self.limit_ns = limit_ns
        self.detail = detail
        parts = [code.value]
        if observed_ns is not None:
            parts.append(f"observed_ns={observed_ns}")
        if limit_ns is not None:
            parts.append(f"limit_ns={limit_ns}")
        if detail:
            parts.append(detail)
        super().__init__(": ".join(parts))


@dataclass(frozen=True, slots=True)
class ObservationConstraints:
    max_camera_span_ns: int = 40_000_000
    max_arm_age_ns: int = 2_000_000
    max_snapshot_age_ns: int = 100_000_000

    def __post_init__(self) -> None:
        if min(
            self.max_camera_span_ns,
            self.max_arm_age_ns,
            self.max_snapshot_age_ns,
        ) <= 0:
            raise ValueError("observation timing limits must be positive")


@dataclass(frozen=True, slots=True)
class RgbFrame:
    data: bytes
    stamp_ns: int
    width: int = PI05_IMAGE_WIDTH
    height: int = PI05_IMAGE_HEIGHT

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.stamp_ns < 0:
            raise ValueError("RGB frame dimensions and timestamp are invalid")
        if len(self.data) != self.width * self.height * PI05_IMAGE_CHANNELS:
            raise ValueError("RGB frame buffer size is invalid")


@dataclass(frozen=True, slots=True)
class RawArmSample:
    joint_positions: tuple[float, ...]
    gripper_position: float
    stamp_ns: int

    def __post_init__(self) -> None:
        if len(self.joint_positions) != 6:
            raise ValueError("arm sample must contain six joint positions")
        if self.stamp_ns < 0:
            raise ValueError("arm stamp must not be negative")
        if not all(
            math.isfinite(value)
            for value in (*self.joint_positions, self.gripper_position)
        ):
            raise ValueError("arm sample values must be finite")


@dataclass(frozen=True, slots=True)
class VlaObservationStep:
    cutoff_ns: int
    camera_left: RgbFrame
    camera_overview: RgbFrame
    camera_right: RgbFrame
    left_arm: RawArmSample
    right_arm: RawArmSample

    def __post_init__(self) -> None:
        stamps = (
            self.camera_left.stamp_ns,
            self.camera_overview.stamp_ns,
            self.camera_right.stamp_ns,
            self.left_arm.stamp_ns,
            self.right_arm.stamp_ns,
        )
        if self.cutoff_ns < 0 or any(stamp > self.cutoff_ns for stamp in stamps):
            raise ValueError("observation source stamp exceeds cutoff")


@dataclass(frozen=True, slots=True)
class Pi05Observation:
    state: tuple[float, ...]
    camera_high: RgbFrame
    camera_left_wrist: RgbFrame
    camera_right_wrist: RgbFrame
    cutoff_ns: int

    def __post_init__(self) -> None:
        if len(self.state) != 14 or not all(math.isfinite(value) for value in self.state):
            raise ValueError("pi0.5 state must contain 14 finite values")
        if self.cutoff_ns < 0:
            raise ValueError("observation cutoff must not be negative")


class Pi05ObservationEncoder:
    def __init__(self, grippers: GripperCalibration) -> None:
        self.grippers = grippers

    def encode(self, step: VlaObservationStep) -> Pi05Observation:
        state = (
            *step.left_arm.joint_positions,
            self.grippers.normalize(step.left_arm.gripper_position),
            *step.right_arm.joint_positions,
            self.grippers.normalize(step.right_arm.gripper_position),
        )
        return Pi05Observation(
            state=state,
            camera_high=step.camera_overview,
            camera_left_wrist=step.camera_left,
            camera_right_wrist=step.camera_right,
            cutoff_ns=step.cutoff_ns,
        )
