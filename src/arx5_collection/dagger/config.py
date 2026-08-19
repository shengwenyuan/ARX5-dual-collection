from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from .observation import GripperCalibration, ObservationConstraints
from .models import DEFAULT_PI05_EXECUTION_PROFILE, PolicyExecutionProfile
from arx5_collection.production.profiles import (
    ArmRuntimeProfile,
    resolve_arm_profile,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DaggerCollectorSettings:
    server_host: str
    server_port: int
    inference_timeout_s: float
    checkpoint_sha256: str
    prompt: str
    grippers: GripperCalibration
    observation: ObservationConstraints
    snapshot_service_timeout_s: float
    execution: PolicyExecutionProfile
    arm_profile: ArmRuntimeProfile

    @classmethod
    def load(cls, path: str | Path) -> DaggerCollectorSettings:
        with Path(path).open("rb") as stream:
            payload = tomllib.load(stream)
        policy = _table(payload, "policy")
        collector = _table(payload, "collector")
        gripper = _table(payload, "gripper")
        observation = _optional_table(payload, "observation")
        robot = _optional_table(payload, "robot")
        settings = cls(
            server_host=str(collector.get("server_host", "127.0.0.1")),
            server_port=int(collector.get("server_port", policy.get("port", 8000))),
            inference_timeout_s=float(collector.get("inference_timeout_s", 30.0)),
            checkpoint_sha256=str(policy["checkpoint_sha256"]).lower(),
            prompt=str(policy["prompt"]),
            grippers=GripperCalibration(
                left_open_raw=float(gripper["left_open_raw"]),
                left_closed_raw=float(gripper["left_closed_raw"]),
                right_open_raw=float(gripper["right_open_raw"]),
                right_closed_raw=float(gripper["right_closed_raw"]),
            ),
            observation=ObservationConstraints(
                max_camera_span_ns=_milliseconds_ns(
                    observation.get("max_camera_span_ms", 40.0)
                ),
                max_arm_age_ns=_milliseconds_ns(
                    observation.get("max_arm_age_ms", 2.0)
                ),
                max_snapshot_age_ns=_milliseconds_ns(
                    observation.get("max_snapshot_age_ms", 100.0)
                ),
            ),
            snapshot_service_timeout_s=(
                float(observation.get("service_timeout_ms", 250.0)) / 1000.0
            ),
            execution=load_policy_execution_profile(payload),
            arm_profile=resolve_arm_profile(
                str(robot.get("profile", robot.get("arm_state_profile", "dagger")))
            ),
        )
        if not settings.server_host or not settings.prompt:
            raise ValueError("policy server host and prompt must not be empty")
        if not 0 < settings.server_port <= 65535:
            raise ValueError("policy server port is invalid")
        if settings.inference_timeout_s <= 0:
            raise ValueError("inference_timeout_s must be positive")
        if settings.snapshot_service_timeout_s <= 0:
            raise ValueError("snapshot service timeout must be positive")
        if not _SHA256.fullmatch(settings.checkpoint_sha256):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        return settings


def load_policy_execution_profile(
    payload: dict[str, object],
) -> PolicyExecutionProfile:
    policy = _table(payload, "policy")
    robot = _optional_table(payload, "robot")
    defaults = DEFAULT_PI05_EXECUTION_PROFILE
    return PolicyExecutionProfile(
        action_chunk_size=int(
            policy.get("action_chunk_size", defaults.action_chunk_size)
        ),
        action_dimension=int(
            policy.get("action_dimension", defaults.action_dimension)
        ),
        execution_steps=int(policy.get("execution_steps", defaults.execution_steps)),
        control_rate_hz=float(robot.get("rate_hz", defaults.control_rate_hz)),
    )


def _table(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"DAgger policy config must contain a [{name}] table")
    return value


def _optional_table(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"DAgger policy config [{name}] must be a table")
    return value


def _milliseconds_ns(value: object) -> int:
    return int(float(value) * 1_000_000)
