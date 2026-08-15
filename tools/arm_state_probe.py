#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from arx5_collection.metrics import (
    finite_scalar,
    finite_vector,
    split_arm_feedback,
    timing_summary,
)


def read_state(arm: Any) -> dict[str, Any]:
    if arm.fault is not None:
        raise RuntimeError(f"arm fault: {arm.fault}")
    joint_position, position_tail = split_arm_feedback(
        arm.get_joint_positions(), "joint_position"
    )
    joint_velocity, velocity_tail = split_arm_feedback(
        arm.get_joint_velocities(), "joint_velocity"
    )
    joint_current, current_tail = split_arm_feedback(
        arm.get_joint_currents(), "joint_current"
    )
    return {
        "joint_position": joint_position,
        "joint_velocity": joint_velocity,
        "joint_current": joint_current,
        "gripper": {
            "position": finite_scalar(arm.get_gripper_pos(), "gripper_position"),
            "velocity": finite_scalar(arm.get_gripper_vel(), "gripper_velocity"),
            "current": finite_scalar(arm.get_gripper_current(), "gripper_current"),
        },
        "sdk_combined_tail": {
            "position": position_tail,
            "velocity": velocity_tail,
            "current": current_tail,
        },
        "eef_xyzrpy": finite_vector(arm.get_ee_pose_xyzrpy(), 6, "eef_xyzrpy"),
    }


def run(config: dict[str, Any], duration_s: float, sample_hz: float) -> dict[str, Any]:
    from bimanual import SingleArm

    arms: dict[str, Any] = {}
    timestamps_ns: list[int] = []
    latest: dict[str, Any] = {}
    result: dict[str, Any] = {
        "duration_s": duration_s,
        "target_hz": sample_hz,
        "arms": [entry["name"] for entry in config["arms"]],
        "status": "FAILED",
        "api_policy": "constructor + state getters + close only",
    }
    period_ns = round(1e9 / sample_hz)
    started_ns = deadline_ns = time.monotonic_ns()
    try:
        for entry in config["arms"]:
            arm = SingleArm(
                {"can_port": entry["can_interface"], "type": config["sdk_type"]}
            )
            arms[entry["name"]] = arm
        result["joint_names"] = {
            name: list(arm.get_joint_names()) for name, arm in arms.items()
        }
        while True:
            now_ns = time.monotonic_ns()
            latest = {name: read_state(arm) for name, arm in arms.items()}
            timestamps_ns.append(now_ns)
            if now_ns - started_ns >= duration_s * 1e9:
                break
            deadline_ns += period_ns
            delay_ns = deadline_ns - time.monotonic_ns()
            if delay_ns > 0:
                time.sleep(delay_ns / 1e9)
        result.update(timing_summary(timestamps_ns))
        result["latest"] = latest
        result["status"] = "PASSED"
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        close_errors = []
        for name, arm in arms.items():
            try:
                arm.close()
            except Exception as error:
                close_errors.append(f"{name}: {error}")
        if close_errors:
            result["status"] = "FAILED"
            result["close_errors"] = close_errors
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--sample-hz", type=float, default=60.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    result = run(config, args.duration, args.sample_hz)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    raise SystemExit(0 if result["status"] == "PASSED" else 1)


if __name__ == "__main__":
    main()
