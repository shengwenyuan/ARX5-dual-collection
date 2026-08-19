from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum


PI05_V2_ACTION_HORIZON = 50
PI05_V2_ACTION_DIMENSION = 14
PI05_V2_EXECUTION_STEPS = 10
PI05_V2_CONTROL_RATE_HZ = 30.0
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
class InferenceTicket:
    inference_id: str
    control_epoch: int
    checkpoint_sha256: str
    action_chunk: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if not self.inference_id:
            raise ValueError("inference_id must not be empty")
        if self.control_epoch < 0:
            raise ValueError("inference control_epoch must not be negative")
        normalized = self.checkpoint_sha256.lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "checkpoint_sha256", normalized)
        if len(self.action_chunk) != PI05_V2_ACTION_HORIZON:
            raise ValueError(
                f"pi0.5 v2 action chunk must contain {PI05_V2_ACTION_HORIZON} steps"
            )
        for action in self.action_chunk:
            if len(action) != PI05_V2_ACTION_DIMENSION:
                raise ValueError(
                    f"pi0.5 v2 action must contain {PI05_V2_ACTION_DIMENSION} values"
                )
            if not all(math.isfinite(value) for value in action):
                raise ValueError("pi0.5 v2 action values must be finite")

    @property
    def execution_chunk(self) -> tuple[tuple[float, ...], ...]:
        return self.action_chunk[:PI05_V2_EXECUTION_STEPS]
