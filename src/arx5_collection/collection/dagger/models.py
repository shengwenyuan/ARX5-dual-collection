from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

from arx5_collection.common.gripper import ARX5_GRIPPER_CONTRACT_ID
from arx5_collection.common.specs import PI05_ARX5_SPEC


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DaggerTriggerEvent(str, Enum):
    RECORD_TOGGLE = "record_toggle"
    OWNERSHIP_TOGGLE = "ownership_toggle"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class DaggerTriggerSignal:
    event: DaggerTriggerEvent
    monotonic_time_ns: int

    def __post_init__(self) -> None:
        if self.monotonic_time_ns < 0:
            raise ValueError("monotonic_time_ns must not be negative")


class ShadowFailureCode(str, Enum):
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    POLICY_TIMEOUT = "policy_timeout"
    POLICY_TRANSPORT_ERROR = "policy_transport_error"
    POLICY_ERROR = "policy_error"


@dataclass(frozen=True, slots=True)
class PolicyExecutionProfile:
    action_chunk_size: int
    action_dimension: int
    execution_steps: int
    control_rate_hz: float

    def __post_init__(self) -> None:
        if self.action_chunk_size <= 0:
            raise ValueError("action_chunk_size must be positive")
        if self.action_dimension <= 0:
            raise ValueError("action_dimension must be positive")
        if not 0 < self.execution_steps <= self.action_chunk_size:
            raise ValueError("execution_steps must be within the action chunk")
        if not math.isfinite(self.control_rate_hz) or self.control_rate_hz <= 0:
            raise ValueError("control_rate_hz must be positive and finite")

    @property
    def inference_period_s(self) -> float:
        return self.execution_steps / self.control_rate_hz


@dataclass(frozen=True, slots=True)
class Pi05InputProfile:
    width: int
    height: int
    channels: int
    layout: str
    color: str
    dtype: str
    resize: str
    crop: str
    pad: str
    model_width: int
    model_height: int
    model_resize: str
    camera_high_source: str
    camera_left_wrist_source: str
    camera_right_wrist_source: str

    def __post_init__(self) -> None:
        if (
            min(
                self.width,
                self.height,
                self.channels,
                self.model_width,
                self.model_height,
            )
            <= 0
        ):
            raise ValueError("PI input dimensions must be positive")
        if self.layout != "chw" or self.color != "rgb" or self.dtype != "uint8":
            raise ValueError("only the accepted RGB uint8 CHW PI input is supported")
        if self.resize != "inter_area":
            raise ValueError("only the accepted INTER_AREA resize is supported")
        if self.crop != "none" or self.pad != "none":
            raise ValueError("collector-side crop and pad must be disabled")
        if self.model_resize != "resize_with_pad":
            raise ValueError("unsupported PI model resize contract")
        camera_sources = (
            self.camera_high_source,
            self.camera_left_wrist_source,
            self.camera_right_wrist_source,
        )
        if (
            any(not source for source in camera_sources)
            or len(set(camera_sources)) != 3
        ):
            raise ValueError("PI camera sources must be three distinct roles")


@dataclass(frozen=True, slots=True)
class Pi05CheckpointProfile:
    policy_type: str
    execution: PolicyExecutionProfile
    max_delay_steps: int
    flow_steps: int
    action_semantics: str
    prefix_mode: str
    input: Pi05InputProfile
    hard_prefix_tolerance: float = 1e-5
    model_action_dimension: int = PI05_ARX5_SPEC.dataset.model_action_dimension
    gripper_normalization: str = "linear_open_closed_0_1"
    gripper_contract: str = ARX5_GRIPPER_CONTRACT_ID

    def __post_init__(self) -> None:
        if self.policy_type not in {"sequential", "training_time_rtc"}:
            raise ValueError("unsupported PI policy type")
        if self.max_delay_steps < 0 or self.flow_steps <= 0:
            raise ValueError("PI delay and flow-step contract is invalid")
        if self.model_action_dimension < self.execution.action_dimension:
            raise ValueError(
                "model action dimension cannot be smaller than robot action"
            )
        if self.action_semantics != "absolute_joint":
            raise ValueError("only absolute joint actions are supported")
        if self.gripper_normalization != "linear_open_closed_0_1":
            raise ValueError("unsupported gripper normalization contract")
        if self.gripper_contract != ARX5_GRIPPER_CONTRACT_ID:
            raise ValueError("unsupported ARX5 gripper contract")
        if self.policy_type == "training_time_rtc":
            if self.max_delay_steps <= 0 or self.prefix_mode != "hard_prefix":
                raise ValueError("training-time RTC requires hard-prefix delay")
            if (
                not math.isfinite(self.hard_prefix_tolerance)
                or self.hard_prefix_tolerance <= 0
            ):
                raise ValueError("hard-prefix tolerance must be positive and finite")


@dataclass(frozen=True, slots=True)
class RtcRolloutProfile:
    prefetch_after_steps: int
    initial_delay_steps: int
    delay_history_size: int
    delay_estimator: str

    def __post_init__(self) -> None:
        if (
            min(
                self.prefetch_after_steps,
                self.delay_history_size,
            )
            <= 0
        ):
            raise ValueError("RTC rollout step counts must be positive")
        if self.initial_delay_steps < 0:
            raise ValueError("RTC initial delay must not be negative")
        if self.delay_estimator != "rolling_max":
            raise ValueError(
                "only the accepted rolling-max delay estimator is supported"
            )

    def validate_for(self, checkpoint: Pi05CheckpointProfile) -> None:
        if checkpoint.policy_type != "training_time_rtc":
            raise ValueError("RTC rollout requires a training-time RTC checkpoint")
        if self.initial_delay_steps >= checkpoint.max_delay_steps:
            raise ValueError("RTC initial delay is outside the trained delay range")
        if self.prefetch_after_steps + checkpoint.max_delay_steps - 1 > (
            checkpoint.execution.action_chunk_size
        ):
            raise ValueError("RTC safe window exceeds the action horizon")

    def safe_window_steps(self, checkpoint: Pi05CheckpointProfile) -> int:
        self.validate_for(checkpoint)
        return self.prefetch_after_steps + checkpoint.max_delay_steps - 1


DEFAULT_PI05_EXECUTION_PROFILE = PolicyExecutionProfile(
    action_chunk_size=PI05_ARX5_SPEC.dataset.action_horizon,
    action_dimension=len(PI05_ARX5_SPEC.dataset.motor_names),
    execution_steps=PI05_ARX5_SPEC.dataset.sequential_execution_steps,
    control_rate_hz=PI05_ARX5_SPEC.dataset.control_rate_hz,
)


@dataclass(frozen=True, slots=True)
class InferenceTiming:
    snapshot_ms: float
    encode_ms: float
    policy_round_trip_ms: float
    server_inference_ms: float
    total_ms: float

    def __post_init__(self) -> None:
        values = (
            self.snapshot_ms,
            self.encode_ms,
            self.policy_round_trip_ms,
            self.server_inference_ms,
            self.total_ms,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("inference timings must be non-negative and finite")


@dataclass(frozen=True, slots=True)
class InferenceTicket:
    inference_id: str
    control_epoch: int
    checkpoint_sha256: str
    action_chunk: tuple[tuple[float, ...], ...]
    execution: PolicyExecutionProfile = DEFAULT_PI05_EXECUTION_PROFILE
    timing: InferenceTiming | None = None

    def __post_init__(self) -> None:
        if not self.inference_id:
            raise ValueError("inference_id must not be empty")
        if self.control_epoch < 0:
            raise ValueError("inference control_epoch must not be negative")
        normalized = self.checkpoint_sha256.lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "checkpoint_sha256", normalized)
        if len(self.action_chunk) != self.execution.action_chunk_size:
            raise ValueError(
                "action chunk must contain " f"{self.execution.action_chunk_size} steps"
            )
        for action in self.action_chunk:
            if len(action) != self.execution.action_dimension:
                raise ValueError(
                    "action must contain " f"{self.execution.action_dimension} values"
                )
            if not all(math.isfinite(value) for value in action):
                raise ValueError("action values must be finite")

    @property
    def execution_chunk(self) -> tuple[tuple[float, ...], ...]:
        return self.action_chunk[: self.execution.execution_steps]
