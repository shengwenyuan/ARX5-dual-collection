from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DaggerTriggerEvent(str, Enum):
    RECORD_TOGGLE = "record_toggle"
    OWNERSHIP_TOGGLE = "ownership_toggle"
    ABORT = "abort"


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


DEFAULT_PI05_EXECUTION_PROFILE = PolicyExecutionProfile(
    action_chunk_size=50,
    action_dimension=14,
    execution_steps=10,
    control_rate_hz=25.0,
)


@dataclass(frozen=True, slots=True)
class InferenceTicket:
    inference_id: str
    control_epoch: int
    checkpoint_sha256: str
    action_chunk: tuple[tuple[float, ...], ...]
    execution: PolicyExecutionProfile = DEFAULT_PI05_EXECUTION_PROFILE

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
                "action chunk must contain "
                f"{self.execution.action_chunk_size} steps"
            )
        for action in self.action_chunk:
            if len(action) != self.execution.action_dimension:
                raise ValueError(
                    "action must contain "
                    f"{self.execution.action_dimension} values"
                )
            if not all(math.isfinite(value) for value in action):
                raise ValueError("action values must be finite")

    @property
    def execution_chunk(self) -> tuple[tuple[float, ...], ...]:
        return self.action_chunk[: self.execution.execution_steps]
