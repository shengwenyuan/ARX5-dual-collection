from __future__ import annotations

from pathlib import Path
from typing import Any

from arx5_collection.pi05_dataset.lerobot_contract import ACTION_HORIZON
from arx5_collection.pi05_dataset.lerobot_contract import CAMERA_KEYS
from arx5_collection.pi05_dataset.lerobot_contract import DATASET_FPS
from arx5_collection.pi05_dataset.lerobot_contract import IMAGE_SIZE
from arx5_collection.pi05_dataset.lerobot_contract import MOTOR_NAMES

EXPECTED_IMAGE_KEYS = {f"observation.images.{camera}" for camera in CAMERA_KEYS}
EXPECTED_KEYS = EXPECTED_IMAGE_KEYS | {
    "observation.state",
    "action",
}


def validate_lerobot(
    dataset_root: Path,
    repo_id: str,
    action_horizon: int = ACTION_HORIZON,
    expected_task: str | None = None,
) -> dict[str, Any]:
    import numpy as np
    from lerobot.common.datasets.lerobot_dataset import LeRobotDataset

    dataset = LeRobotDataset(
        repo_id,
        root=dataset_root,
        delta_timestamps={"action": [step / DATASET_FPS for step in range(action_horizon)]},
        download_videos=False,
        video_backend="pyav",
    )
    missing = EXPECTED_KEYS - set(dataset.features)
    if missing:
        raise ValueError(f"LeRobot dataset is missing π0.5 fields: {sorted(missing)}")
    if dataset.fps != DATASET_FPS:
        raise ValueError(f"LeRobot fps must be {DATASET_FPS}, got {dataset.fps}")
    joint_action_dim = len(MOTOR_NAMES)
    if tuple(dataset.features["observation.state"]["shape"]) != (joint_action_dim,):
        raise ValueError(f"observation.state must have shape [{joint_action_dim}]")
    if tuple(dataset.features["action"]["shape"]) != (joint_action_dim,):
        raise ValueError(f"action must have shape [{joint_action_dim}]")
    if dataset.num_frames == 0 or dataset.num_episodes == 0:
        raise ValueError("LeRobot dataset is empty")

    checked = sorted({0, dataset.num_frames // 2, dataset.num_frames - 1})
    checked_tasks = set()
    for index in checked:
        sample = dataset[index]
        if np.asarray(sample["observation.state"]).shape != (joint_action_dim,):
            raise ValueError(f"invalid state at dataset index {index}")
        if np.asarray(sample["action"]).shape != (action_horizon, joint_action_dim):
            raise ValueError(f"invalid action chunk at dataset index {index}")
        width, height = IMAGE_SIZE
        for camera in EXPECTED_IMAGE_KEYS:
            if np.asarray(sample[camera]).shape != (3, height, width):
                raise ValueError(f"invalid {camera} at dataset index {index}")
        task = sample.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"missing task at dataset index {index}")
        checked_tasks.add(task)
    if expected_task is not None and checked_tasks != {expected_task}:
        raise ValueError(
            f"unexpected task values: expected {expected_task!r}, got {sorted(checked_tasks)!r}"
        )
    return {
        "status": "lerobot_ready",
        "repo_id": repo_id,
        "dataset_root": str(dataset_root.resolve()),
        "fps": dataset.fps,
        "episodes": dataset.num_episodes,
        "frames": dataset.num_frames,
        "action_horizon": action_horizon,
        "checked_indices": checked,
        "checked_tasks": sorted(checked_tasks),
    }
