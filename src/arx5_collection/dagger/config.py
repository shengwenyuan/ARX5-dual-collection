from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from .action_gateway import JointActionSafety
from .observation import GripperCalibration, ObservationConstraints
from .models import (
    DEFAULT_PI05_EXECUTION_PROFILE,
    Pi05CheckpointProfile,
    Pi05InputProfile,
    PolicyExecutionProfile,
    RtcRolloutProfile,
)
from arx5_collection.production.profiles import (
    ArmRuntimeProfile,
    resolve_arm_profile,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DaggerControlSettings:
    safety: JointActionSafety
    state_timeout_s: float
    policy_wait_timeout_s: float
    command_watchdog_s: float

    def __post_init__(self) -> None:
        if min(
            self.state_timeout_s,
            self.policy_wait_timeout_s,
            self.command_watchdog_s,
        ) <= 0:
            raise ValueError("DAgger control timeouts must be positive")


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
        gripper = _table(payload, "gripper")
        observation = _optional_table(payload, "observation")
        robot = _optional_table(payload, "robot")
        safety = _optional_table(payload, "safety")
        gateway = _optional_table(payload, "gateway")
        checkpoint_profile = load_checkpoint_profile(payload)
        rollout = load_rtc_rollout(payload, checkpoint_profile)
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
            execution=checkpoint_profile.execution,
            checkpoint_profile=checkpoint_profile,
            rtc_rollout=rollout,
            arm_profile=resolve_arm_profile(
                str(robot.get("profile", robot.get("arm_state_profile", "dagger")))
            ),
            control=DaggerControlSettings(
                safety=JointActionSafety(
                    max_joint_step_rad=float(
                        safety.get("max_joint_step_rad", 0.25)
                    ),
                    max_joint_departure_rad=float(
                        safety.get("max_joint_departure_rad", 1.5)
                    ),
                    min_normalized_gripper=float(
                        safety.get("min_normalized_gripper", 0.0)
                    ),
                    max_normalized_gripper=float(
                        safety.get("max_normalized_gripper", 1.0)
                    ),
                ),
                state_timeout_s=float(gateway.get("state_timeout_s", 0.1)),
                policy_wait_timeout_s=float(
                    gateway.get("policy_wait_timeout_s", 0.5)
                ),
                command_watchdog_s=float(
                    gateway.get("command_watchdog_s", 0.12)
                ),
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
    checkpoint = _optional_table(payload, "checkpoint_profile")
    robot = _optional_table(payload, "robot")
    defaults = DEFAULT_PI05_EXECUTION_PROFILE
    return PolicyExecutionProfile(
        action_chunk_size=int(
            checkpoint.get(
                "action_horizon",
                policy.get("action_chunk_size", defaults.action_chunk_size),
            )
        ),
        action_dimension=int(
            checkpoint.get(
                "action_dimension",
                policy.get("action_dimension", defaults.action_dimension),
            )
        ),
        execution_steps=int(
            checkpoint.get(
                "sequential_execution_steps",
                policy.get("execution_steps", defaults.execution_steps),
            )
        ),
        control_rate_hz=float(
            checkpoint.get(
                "control_rate_hz",
                robot.get("rate_hz", defaults.control_rate_hz),
            )
        ),
    )


def load_checkpoint_profile(payload: dict[str, object]) -> Pi05CheckpointProfile:
    checkpoint = _optional_table(payload, "checkpoint_profile")
    execution = load_policy_execution_profile(payload)
    if not checkpoint:
        return Pi05CheckpointProfile(
            policy_type="sequential",
            execution=execution,
            max_delay_steps=0,
            flow_steps=10,
            action_semantics="absolute_joint",
            prefix_mode="none",
            input=Pi05InputProfile(
                width=640,
                height=360,
                channels=3,
                layout="chw",
                color="rgb",
                dtype="uint8",
                resize="inter_area",
                crop="none",
                pad="none",
                model_width=224,
                model_height=224,
                model_resize="resize_with_pad",
                camera_high_source="overview",
                camera_left_wrist_source="left",
                camera_right_wrist_source="right",
            ),
        )
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


def _milliseconds_ns(value: object) -> int:
    return int(float(value) * 1_000_000)
