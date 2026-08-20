from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
import re
import time
import tomllib
from typing import Any

from .checkpoint import checkpoint_tree_sha256
from .config import load_checkpoint_profile, load_rtc_rollout
from .models import (
    InferenceTicket,
    Pi05CheckpointProfile,
    PolicyExecutionProfile,
    RtcRolloutProfile,
)
from .policy_envelope import CorrelatedPolicyEnvelope


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PolicyServerSettings:
    checkpoint: Path
    checkpoint_sha256: str
    repo_id: str
    prompt: str
    host: str
    port: int
    base_checkpoint: str
    execution: PolicyExecutionProfile
    checkpoint_profile: Pi05CheckpointProfile
    rtc_rollout: RtcRolloutProfile | None

    @classmethod
    def load(cls, path: str | Path) -> PolicyServerSettings:
        config_path = Path(path)
        with config_path.open("rb") as stream:
            payload = tomllib.load(stream)
        policy = payload.get("policy")
        if not isinstance(policy, dict):
            raise ValueError("policy config must contain a [policy] table")
        checkpoint_profile = load_checkpoint_profile(payload)
        settings = cls(
            checkpoint=Path(str(policy["checkpoint"])),
            checkpoint_sha256=str(policy["checkpoint_sha256"]).lower(),
            repo_id=str(policy["repo_id"]),
            prompt=str(policy["prompt"]),
            host=str(policy.get("host", "0.0.0.0")),
            port=int(policy.get("port", 8000)),
            base_checkpoint=str(
                policy.get(
                    "base_checkpoint",
                    "gs://openpi-assets/checkpoints/pi05_base/params",
                )
            ),
            checkpoint_profile=checkpoint_profile,
            rtc_rollout=load_rtc_rollout(payload, checkpoint_profile),
            execution=checkpoint_profile.execution,
        )
        if not settings.repo_id or not settings.prompt or not settings.host:
            raise ValueError("repo_id, prompt, and host must not be empty")
        if not 0 < settings.port <= 65535:
            raise ValueError("policy port is invalid")
        if not _SHA256.fullmatch(settings.checkpoint_sha256):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        return settings


def create_pi05_joint_policy(settings: PolicyServerSettings):
    """Create the joint-space π0.5 policy contract used by ARX5 checkpoints."""
    from openpi import transforms
    from openpi.models import pi0_config
    from openpi.policies import policy_config
    from openpi.training import config as training_config
    from openpi.training import weight_loaders

    repack = transforms.Group(
        inputs=[
            transforms.RepackTransform(
                {
                    "images": {
                        "cam_high": "observation.images.cam_high",
                        "cam_left_wrist": "observation.images.cam_left_wrist",
                        "cam_right_wrist": "observation.images.cam_right_wrist",
                    },
                    "state": "observation.state",
                    "actions": "action",
                    "prompt": "prompt",
                }
            )
        ]
    )
    data_kwargs: dict[str, Any] = {}
    if settings.checkpoint_profile.policy_type == "training_time_rtc":
        data_kwargs["use_delta_joint_actions"] = True
    data = training_config.LeRobotAlohaDataConfig(
        repo_id=settings.repo_id,
        adapt_to_pi=False,
        repack_transforms=repack,
        base_config=training_config.DataConfig(prompt_from_task=True),
        **data_kwargs,
    )
    model = pi0_config.Pi0Config(
        pi05=True,
        action_dim=settings.checkpoint_profile.model_action_dimension,
        action_horizon=settings.execution.action_chunk_size,
    )
    policy_metadata: dict[str, Any] = {}
    if settings.checkpoint_profile.policy_type == "training_time_rtc":
        from openpi.models import pi0_rtc_config

        model = pi0_rtc_config.Pi05RtcConfig(
            action_dim=settings.checkpoint_profile.model_action_dimension,
            action_horizon=settings.execution.action_chunk_size,
            max_delay=settings.checkpoint_profile.max_delay_steps,
        )
        policy_metadata = {
            "policy_metadata": {
                "policy_type": settings.checkpoint_profile.policy_type,
                "max_delay": settings.checkpoint_profile.max_delay_steps,
            }
        }
    train = training_config.TrainConfig(
        name="pi05_arx5_joint_sft",
        model=model,
        data=data,
        weight_loader=weight_loaders.CheckpointWeightLoader(settings.base_checkpoint),
        assets_base_dir="./assets",
        checkpoint_base_dir="./checkpoints",
        batch_size=64,
        fsdp_devices=1,
        num_workers=1,
        **policy_metadata,
    )
    policy = policy_config.create_trained_policy(
        train,
        settings.checkpoint,
        default_prompt=settings.prompt,
    )
    if settings.checkpoint_profile.policy_type == "training_time_rtc":
        from .training_time_rtc_policy import TrainingTimeRtcPolicyAdapter

        policy = TrainingTimeRtcPolicyAdapter(
            policy,
            action_horizon=settings.execution.action_chunk_size,
            action_dimension=settings.execution.action_dimension,
            max_delay_steps=settings.checkpoint_profile.max_delay_steps,
            flow_steps=settings.checkpoint_profile.flow_steps,
        )
    return policy


def warm_up_pi05_policy(
    policy: Any,
    prompt: str,
    checkpoint: Pi05CheckpointProfile,
    rollout: RtcRolloutProfile | None,
    numpy_module: Any | None = None,
) -> None:
    """Compile the accepted π0.5 input shape before the server becomes healthy."""
    if numpy_module is None:
        import numpy as numpy_module

    image = numpy_module.zeros(
        (
            checkpoint.input.channels,
            checkpoint.input.height,
            checkpoint.input.width,
        ),
        dtype=numpy_module.uint8,
    )
    result = policy.infer(
        {
            "state": numpy_module.zeros(
                checkpoint.execution.action_dimension,
                dtype=numpy_module.float32,
            ),
            "images": {
                "cam_high": image,
                "cam_left_wrist": image,
                "cam_right_wrist": image,
            },
            "prompt": prompt,
        }
    )
    try:
        action_chunk = tuple(
            tuple(float(value) for value in row) for row in result["actions"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"π0.5 warm-up returned an invalid action: {error}") from error
    InferenceTicket(
        "policy-warmup",
        0,
        "0" * 64,
        action_chunk,
        checkpoint.execution,
    )
    if checkpoint.policy_type == "training_time_rtc":
        if rollout is None:
            raise RuntimeError("RTC warm-up requires a rollout profile")
        delay = rollout.initial_delay_steps
        conditioned = policy.infer_rtc(
            {
                "state": numpy_module.zeros(
                    checkpoint.execution.action_dimension,
                    dtype=numpy_module.float32,
                ),
                "images": {
                    "cam_high": image,
                    "cam_left_wrist": image,
                    "cam_right_wrist": image,
                },
                "prompt": prompt,
            },
            {
                "estimated_delay_steps": delay,
                "action_prefix": action_chunk[:delay],
            },
        )
        try:
            conditioned_chunk = tuple(
                tuple(float(value) for value in row)
                for row in conditioned["actions"]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"π0.5 RTC warm-up returned an invalid action: {error}"
            ) from error
        InferenceTicket(
            "policy-rtc-warmup",
            0,
            "0" * 64,
            conditioned_chunk,
            checkpoint.execution,
        )
    logging.info("Policy warm-up complete")


def serve(settings: PolicyServerSettings) -> None:
    from openpi.serving.websocket_policy_server import WebsocketPolicyServer

    logging.info("Hashing configured checkpoint once: %s", settings.checkpoint)
    actual_sha256 = checkpoint_tree_sha256(settings.checkpoint)
    if actual_sha256 != settings.checkpoint_sha256:
        raise RuntimeError(
            "configured checkpoint SHA-256 mismatch: "
            f"expected={settings.checkpoint_sha256}, actual={actual_sha256}"
        )
    logging.info("Checkpoint identity verified: %s", actual_sha256)
    policy = create_pi05_joint_policy(settings)
    warm_up_pi05_policy(
        policy,
        settings.prompt,
        settings.checkpoint_profile,
        settings.rtc_rollout,
    )
    envelope = CorrelatedPolicyEnvelope(policy, actual_sha256, time.time_ns)
    WebsocketPolicyServer(
        envelope,
        host=settings.host,
        port=settings.port,
        metadata={
            "service": "arx5-dagger-policy",
            "checkpoint_sha256": actual_sha256,
            "action_horizon": settings.execution.action_chunk_size,
            "action_dimension": settings.execution.action_dimension,
            "execution_steps": settings.execution.execution_steps,
            "control_rate_hz": settings.execution.control_rate_hz,
            "policy_type": settings.checkpoint_profile.policy_type,
            "max_delay_steps": settings.checkpoint_profile.max_delay_steps,
            "flow_steps": settings.checkpoint_profile.flow_steps,
            "input_width": settings.checkpoint_profile.input.width,
            "input_height": settings.checkpoint_profile.input.height,
            "input_channels": settings.checkpoint_profile.input.channels,
            "input_layout": settings.checkpoint_profile.input.layout,
            "input_color": settings.checkpoint_profile.input.color,
            "input_dtype": settings.checkpoint_profile.input.dtype,
            "input_resize": settings.checkpoint_profile.input.resize,
            "action_semantics": settings.checkpoint_profile.action_semantics,
            "prefix_mode": settings.checkpoint_profile.prefix_mode,
            "hard_prefix_tolerance": (
                settings.checkpoint_profile.hard_prefix_tolerance
            ),
            "model_action_dimension": (
                settings.checkpoint_profile.model_action_dimension
            ),
            "gripper_normalization": (
                settings.checkpoint_profile.gripper_normalization
            ),
            "model_input_width": settings.checkpoint_profile.input.model_width,
            "model_input_height": settings.checkpoint_profile.input.model_height,
            "model_input_resize": settings.checkpoint_profile.input.model_resize,
            "input_crop": settings.checkpoint_profile.input.crop,
            "input_pad": settings.checkpoint_profile.input.pad,
            "camera_high_source": (
                settings.checkpoint_profile.input.camera_high_source
            ),
            "camera_left_wrist_source": (
                settings.checkpoint_profile.input.camera_left_wrist_source
            ),
            "camera_right_wrist_source": (
                settings.checkpoint_profile.input.camera_right_wrist_source
            ),
        },
    ).serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve a correlated ARX5 π0.5 policy")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    serve(PolicyServerSettings.load(args.config))


if __name__ == "__main__":
    main()
