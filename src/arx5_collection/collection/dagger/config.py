from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import tomllib

from .action_gateway import JointActionSafety
from .observation import ObservationConstraints
from .models import (
    Pi05CheckpointProfile,
    Pi05InputProfile,
    PolicyExecutionProfile,
    RtcRolloutProfile,
)
from arx5_collection.collection.runtime.profiles import (
    ArmRuntimeProfile,
    resolve_arm_profile,
)
from arx5_collection.common.gripper import ARX5_GRIPPER_CALIBRATION
from arx5_collection.common.gripper import ARX5_GRIPPER_CONTRACT_ID
from arx5_collection.common.gripper import GripperCalibration


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DaggerControlSettings:
    safety: JointActionSafety
    state_timeout_s: float
    policy_wait_timeout_s: float
    command_watchdog_s: float
    rtc_deadline_margin_s: float

    def __post_init__(self) -> None:
        if (
            min(
                self.state_timeout_s,
                self.policy_wait_timeout_s,
                self.command_watchdog_s,
                self.rtc_deadline_margin_s,
            )
            <= 0
        ):
            raise ValueError("DAgger control timeouts must be positive")


@dataclass(frozen=True, slots=True)
class DaggerCollectorSettings:
    server_host: str
    server_port: int
    inference_timeout_s: float
    checkpoint_sha256: str
    prompt: str
    gripper_contract: str
    gripper_action_offset: float
    grippers: GripperCalibration
    observation: ObservationConstraints
    snapshot_timeout_s: float
    execution: PolicyExecutionProfile
    checkpoint_profile: Pi05CheckpointProfile
    rtc_rollout: RtcRolloutProfile | None
    arm_profile: ArmRuntimeProfile
    control: DaggerControlSettings

    @classmethod
    def load(cls, path: str | Path) -> DaggerCollectorSettings:
        with Path(path).open("rb") as stream:
            payload = tomllib.load(stream)
        policy = _table(payload, "policy")
        collector = _table(payload, "collector")
        _allowed_keys(
            collector,
            required={"server_host", "server_port", "inference_timeout_s"},
            optional=set(),
            label="collector",
        )
        gripper = _table(payload, "gripper")
        _allowed_keys(
            gripper,
            required={"contract", "normalized_action_offset"},
            optional=set(),
            label="gripper",
        )
        gripper_contract = str(gripper["contract"])
        gripper_action_offset = float(gripper["normalized_action_offset"])
        if gripper_contract != ARX5_GRIPPER_CONTRACT_ID:
            raise ValueError("unsupported ARX5 gripper contract")
        if not math.isfinite(gripper_action_offset):
            raise ValueError("gripper normalized_action_offset must be finite")
        observation = _table(payload, "observation")
        robot = _table(payload, "robot")
        safety = _table(payload, "safety")
        gateway = _table(payload, "gateway")
        _allowed_keys(
            observation,
            required={
                "max_camera_span_ms",
                "max_arm_age_ms",
                "max_snapshot_age_ms",
                "request_timeout_ms",
            },
            optional=set(),
            label="observation",
        )
        _allowed_keys(robot, required={"profile"}, optional={"rate_hz"}, label="robot")
        _allowed_keys(
            safety,
            required={
                "max_joint_step_rad",
                "max_joint_departure_rad",
                "min_normalized_gripper",
                "max_normalized_gripper",
                "min_policy_gripper",
                "max_policy_gripper",
            },
            optional=set(),
            label="safety",
        )
        _allowed_keys(
            gateway,
            required={
                "state_timeout_s",
                "policy_wait_timeout_s",
                "command_watchdog_s",
                "rtc_deadline_margin_ms",
            },
            optional=set(),
            label="gateway",
        )
        checkpoint_profile = load_checkpoint_profile(payload)
        rollout = load_rtc_rollout(payload, checkpoint_profile)
        settings = cls(
            server_host=str(collector["server_host"]),
            server_port=int(collector["server_port"]),
            inference_timeout_s=float(collector["inference_timeout_s"]),
            checkpoint_sha256=str(policy["checkpoint_sha256"]).lower(),
            prompt=str(policy["prompt"]),
            gripper_contract=gripper_contract,
            gripper_action_offset=gripper_action_offset,
            grippers=ARX5_GRIPPER_CALIBRATION,
            observation=ObservationConstraints(
                max_camera_span_ns=_milliseconds_ns(observation["max_camera_span_ms"]),
                max_arm_age_ns=_milliseconds_ns(observation["max_arm_age_ms"]),
                max_snapshot_age_ns=_milliseconds_ns(
                    observation["max_snapshot_age_ms"]
                ),
            ),
            snapshot_timeout_s=(float(observation["request_timeout_ms"]) / 1000.0),
            execution=checkpoint_profile.execution,
            checkpoint_profile=checkpoint_profile,
            rtc_rollout=rollout,
            arm_profile=resolve_arm_profile(str(robot["profile"])),
            control=DaggerControlSettings(
                safety=JointActionSafety(
                    max_joint_step_rad=float(safety["max_joint_step_rad"]),
                    max_joint_departure_rad=float(safety["max_joint_departure_rad"]),
                    min_normalized_gripper=float(safety["min_normalized_gripper"]),
                    max_normalized_gripper=float(safety["max_normalized_gripper"]),
                    min_policy_gripper=float(safety["min_policy_gripper"]),
                    max_policy_gripper=float(safety["max_policy_gripper"]),
                ),
                state_timeout_s=float(gateway["state_timeout_s"]),
                policy_wait_timeout_s=float(gateway["policy_wait_timeout_s"]),
                command_watchdog_s=float(gateway["command_watchdog_s"]),
                rtc_deadline_margin_s=float(gateway["rtc_deadline_margin_ms"]) / 1000.0,
            ),
        )
        if not settings.server_host or not settings.prompt:
            raise ValueError("policy server host and prompt must not be empty")
        if not 0 < settings.server_port <= 65535:
            raise ValueError("policy server port is invalid")
        if settings.inference_timeout_s <= 0:
            raise ValueError("inference_timeout_s must be positive")
        if settings.snapshot_timeout_s <= 0:
            raise ValueError("snapshot request timeout must be positive")
        if not _SHA256.fullmatch(settings.checkpoint_sha256):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        if settings.checkpoint_profile.gripper_contract != settings.gripper_contract:
            raise ValueError("checkpoint and runtime gripper contracts do not match")
        _validate_rtc_deadline(settings)
        return settings


def _validate_rtc_deadline(settings: DaggerCollectorSettings) -> None:
    checkpoint = settings.checkpoint_profile
    if checkpoint.policy_type != "training_time_rtc":
        return
    hard_deadline_s = checkpoint.max_delay_steps / checkpoint.execution.control_rate_hz
    request_deadline_s = settings.control.policy_wait_timeout_s
    snapshot_deadline_s = settings.snapshot_timeout_s
    if snapshot_deadline_s >= request_deadline_s:
        raise ValueError("snapshot timeout must be below the RTC request timeout")
    if (
        request_deadline_s + settings.control.rtc_deadline_margin_s
        > hard_deadline_s + 1e-9
    ):
        raise ValueError("RTC request timeout and margin exceed the action deadline")


def load_policy_execution_profile(
    payload: dict[str, object],
) -> PolicyExecutionProfile:
    checkpoint = _table(payload, "checkpoint_profile")
    return PolicyExecutionProfile(
        action_chunk_size=int(checkpoint["action_horizon"]),
        action_dimension=int(checkpoint["action_dimension"]),
        execution_steps=int(checkpoint["sequential_execution_steps"]),
        control_rate_hz=float(checkpoint["control_rate_hz"]),
    )


def load_checkpoint_profile(payload: dict[str, object]) -> Pi05CheckpointProfile:
    checkpoint = _table(payload, "checkpoint_profile")
    execution = load_policy_execution_profile(payload)
    image = _table(payload, "model_input")
    return Pi05CheckpointProfile(
        policy_type=str(checkpoint["policy_type"]),
        execution=execution,
        max_delay_steps=int(checkpoint["max_delay_steps"]),
        flow_steps=int(checkpoint["flow_steps"]),
        action_semantics=str(checkpoint["action_semantics"]),
        prefix_mode=str(checkpoint["prefix_mode"]),
        input=Pi05InputProfile(
            width=int(image["width"]),
            height=int(image["height"]),
            channels=int(image["channels"]),
            layout=str(image["layout"]),
            color=str(image["color"]),
            dtype=str(image["dtype"]),
            resize=str(image["resize"]),
            crop=str(image["crop"]),
            pad=str(image["pad"]),
            model_width=int(image["model_width"]),
            model_height=int(image["model_height"]),
            model_resize=str(image["model_resize"]),
            camera_high_source=str(image["camera_high_source"]),
            camera_left_wrist_source=str(image["camera_left_wrist_source"]),
            camera_right_wrist_source=str(image["camera_right_wrist_source"]),
        ),
        hard_prefix_tolerance=float(checkpoint["hard_prefix_tolerance"]),
        model_action_dimension=int(checkpoint["model_action_dimension"]),
        gripper_normalization=str(checkpoint["gripper_normalization"]),
        gripper_contract=str(checkpoint["gripper_contract"]),
    )


def load_rtc_rollout(
    payload: dict[str, object],
    checkpoint: Pi05CheckpointProfile,
) -> RtcRolloutProfile | None:
    rollout = _optional_table(payload, "rollout")
    if checkpoint.policy_type != "training_time_rtc":
        if rollout:
            raise ValueError("[rollout] is only valid for training-time RTC")
        return None
    if not rollout:
        raise ValueError("training-time RTC config must contain a [rollout] table")
    profile = RtcRolloutProfile(
        prefetch_after_steps=int(rollout["prefetch_after_steps"]),
        initial_delay_steps=int(rollout["initial_delay_steps"]),
        delay_history_size=int(rollout["delay_history_size"]),
        delay_estimator=str(rollout["delay_estimator"]),
    )
    profile.validate_for(checkpoint)
    return profile


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


def _allowed_keys(
    value: dict[str, object],
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    if not required <= set(value) or set(value) - required - optional:
        raise ValueError(
            f"DAgger policy config [{label}] keys must be exactly "
            f"{sorted(required)} plus optional {sorted(optional)}"
        )


def _milliseconds_ns(value: object) -> int:
    return int(float(value) * 1_000_000)
