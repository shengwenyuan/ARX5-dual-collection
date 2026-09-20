"""Translate a checkpoint's SafeInfer configuration at the host boundary.

The resolved TOML is shared by the existing collector and policy service. This
module does not open hardware, import OpenPI, or change the RTC scheduler.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tomllib

from .checkpoint import checkpoint_tree_sha256
from .config import DaggerCollectorSettings
from .policy_server import PolicyServerSettings
from arx5_collection.production.config import load_station_config


def resolve_config(
    source: dict,
    template: dict,
    *,
    checkpoint_metadata: dict,
    checkpoint_sha256: str,
    checkpoint_path: str,
) -> dict:
    policy, rtc = source["policy"], source["rtc"]
    if policy["mode"] != "training_time_rtc":
        raise ValueError("unified config requires training_time_rtc")
    variant = policy.get("model_variant", "standard")
    if variant not in {"standard", "arx5_base"}:
        raise ValueError(f"unsupported model_variant: {variant}")
    if checkpoint_metadata.get("policy_type") != policy["mode"]:
        raise ValueError("checkpoint policy type disagrees with inference config")
    if rtc["prefix_attention_schedule"] != "hard_prefix":
        raise ValueError("only hard_prefix is supported")
    g = source["gripper"]
    for side in ("left", "right"):
        if (g[f"{side}_open_raw"], g[f"{side}_closed_raw"]) != (-3.4, 0.0):
            raise ValueError("gripper calibration disagrees with arx5-gripper-v1")
    if g["opening_offset"] != g["closing_offset"]:
        raise ValueError(
            "direction-dependent gripper offsets require a separate adapter"
        )
    offset = float(g["opening_offset"])
    if not math.isfinite(offset):
        raise ValueError("gripper offset must be finite")
    out = deepcopy(template)
    out["policy"].update(
        checkpoint=checkpoint_path,
        checkpoint_sha256=checkpoint_sha256,
        repo_id=policy["repo_id"],
        prompt=policy["instruction"],
        host="0.0.0.0",
        port=8000,
        model_variant=variant,
        openpi_root="/opt/checkpoint-openpi",
    )
    p = out["checkpoint_profile"]
    p.update(
        action_dimension=14,
        model_action_dimension=32,
        policy_type=policy["mode"],
        action_horizon=int(policy["action_chunk_size"]),
        sequential_execution_steps=int(policy["execution_steps"]),
        control_rate_hz=float(source["robot"]["rate_hz"]),
        max_delay_steps=int(checkpoint_metadata["max_delay"]),
        flow_steps=int(rtc["num_flow_steps"]),
        prefix_mode="hard_prefix",
    )
    out["rollout"].update(
        prefetch_after_steps=int(rtc["minimum_execution_horizon"]),
        initial_delay_steps=int(rtc["initial_delay_steps"]),
        delay_history_size=int(rtc["delay_history_size"]),
    )
    out["model_input"].update(
        width=int(source["cameras"]["width"]), height=int(source["cameras"]["height"])
    )
    out["collector"].update(server_host="127.0.0.1", server_port=8000)
    out["safety"].update(
        {
            k: source["safety"][k]
            for k in ("max_joint_step_rad", "max_joint_departure_rad")
        }
    )
    out["gripper"] = {"contract": "arx5-gripper-v1", "normalized_action_offset": offset}
    # Preserve the station's margin and cap timeouts to the selected frequency.
    rate = p["control_rate_hz"]
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("control frequency must be positive and finite")
    gateway = out["gateway"]
    budget = p["max_delay_steps"] / rate
    margin = float(gateway.get("rtc_deadline_margin_ms", 50.0)) / 1000
    wait = min(float(gateway["policy_wait_timeout_s"]), budget - margin)
    if wait <= 0:
        raise ValueError("RTC deadline has no room after the configured margin")
    gateway["policy_wait_timeout_s"] = wait
    obs = out["observation"]
    obs["request_timeout_ms"] = min(
        float(obs.get("request_timeout_ms", 250.0)), wait * 1000 * 2 / 3
    )
    obs.pop("service_timeout_ms", None)
    return out


def write_toml(path: Path, payload: dict) -> None:
    # All generated settings are scalar values in one-level tables.
    lines = []
    for table, fields in payload.items():
        lines.append(f"[{table}]")
        for key, value in fields.items():
            lines.append(
                f"{key} = {json.dumps(value, ensure_ascii=False, allow_nan=False)}"
            )
        lines.append("")
    path.write_text("\n".join(lines))


def prepare_policy(
    inference_config: Path,
    runtime_config: Path,
    output_dir: Path,
    checkpoint_root: Path,
    station_config: Path,
) -> tuple[Path, Path]:
    source = tomllib.loads(inference_config.read_text())
    template = tomllib.loads(runtime_config.read_text())
    checkpoint_root = checkpoint_root.resolve(strict=True)
    checkpoint = Path(source["policy"]["checkpoint"]).resolve(strict=True)
    relative = checkpoint.relative_to(checkpoint_root)
    openpi = Path(source["policy"]["openpi_root"]).resolve(strict=True)
    variant = source["policy"].get("model_variant", "standard")
    if variant not in {"standard", "arx5_base"}:
        raise ValueError(f"unsupported model_variant: {variant}")
    model_file = (
        "experiments/arx5_base/model_config.py"
        if variant == "arx5_base"
        else "models/pi0_rtc_config.py"
    )
    if not (openpi / "src/openpi" / model_file).is_file():
        raise ValueError("matching OpenPI model source is missing")
    if not (openpi / "packages/openpi-client/src/openpi_client").is_dir():
        raise ValueError("matching OpenPI client source is missing")
    repo_id = Path(source["policy"]["repo_id"])
    norm = checkpoint / "assets" / repo_id / "norm_stats.json"
    if not norm.is_file() or not norm.resolve().is_relative_to(checkpoint / "assets"):
        raise ValueError("checkpoint normalization assets do not match repo_id")
    station = load_station_config(station_config)
    expected_cameras = {
        "overview": "cam_high",
        "left": "cam_left_wrist",
        "right": "cam_right_wrist",
    }
    for camera in station.cameras:
        if camera.serial_number != source["cameras"][expected_cameras[camera.role]]:
            raise ValueError(f"camera serial mismatch for {camera.role}")
    metadata = json.loads((checkpoint / "assets/policy_type.json").read_text())
    payload = resolve_config(
        source,
        template,
        checkpoint_metadata=metadata,
        checkpoint_sha256=checkpoint_tree_sha256(checkpoint),
        checkpoint_path="/checkpoints/" + relative.as_posix(),
    )
    payload["config_source"] = {
        "inference_config": str(inference_config.resolve()),
        "inference_config_sha256": hashlib.sha256(
            inference_config.read_bytes()
        ).hexdigest(),
        "runtime_template": str(runtime_config.resolve()),
        "runtime_template_sha256": hashlib.sha256(
            runtime_config.read_bytes()
        ).hexdigest(),
        "openpi_source": str(openpi),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "policy.toml"
    pending = output_dir / "policy.pending.toml"
    write_toml(pending, payload)
    DaggerCollectorSettings.load(pending)
    PolicyServerSettings.load(pending)
    pending.replace(path)
    # JSON is valid YAML; long bind syntax refuses missing host directories.
    override = output_dir / "compose.policy.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "policy-server": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(openpi),
                                "target": "/opt/checkpoint-openpi",
                                "read_only": True,
                                "bind": {"create_host_path": False},
                            }
                        ]
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )
    return path.resolve(), override.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "inference-config",
        "runtime-config",
        "output-dir",
        "checkpoint-root",
        "station-config",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    for path in prepare_policy(**vars(args)):
        print(path)


if __name__ == "__main__":
    main()
