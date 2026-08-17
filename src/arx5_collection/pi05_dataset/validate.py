from __future__ import annotations

from pathlib import Path
from typing import Any

from arx5_collection.atomic import staged_directory
from arx5_collection.pi05_dataset.openpi_contract import ACTION_HORIZON
from arx5_collection.pi05_dataset.openpi_contract import CAMERA_KEYS
from arx5_collection.pi05_dataset.openpi_contract import DATASET_FPS
from arx5_collection.pi05_dataset.openpi_contract import IMAGE_SIZE
from arx5_collection.pi05_dataset.openpi_contract import MODEL_ACTION_DIM
from arx5_collection.pi05_dataset.openpi_contract import MOTOR_NAMES

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
        task = str(sample.get("task", "")).strip()
        if not task:
            raise ValueError(f"missing task at dataset index {index}")
        checked_tasks.add(task)
    if expected_task is not None and checked_tasks != {expected_task}:
        raise ValueError(
            f"unexpected task values: expected {expected_task!r}, got {sorted(checked_tasks)!r}"
        )
    return {
        "status": "ready_for_openpi_data_adapter",
        "repo_id": repo_id,
        "dataset_root": str(dataset_root.resolve()),
        "fps": dataset.fps,
        "episodes": dataset.num_episodes,
        "frames": dataset.num_frames,
        "action_horizon": action_horizon,
        "checked_indices": checked,
        "checked_tasks": sorted(checked_tasks),
    }


def validate_openpi(dataset_home: Path, repo_id: str) -> dict[str, Any]:
    import os

    os.environ["HF_LEROBOT_HOME"] = str(dataset_home.resolve())

    import numpy as np
    from openpi.models import pi0_config
    from openpi.training import data_loader

    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_data_config

    model = pi0_config.Pi0Config(
        pi05=True,
        action_dim=MODEL_ACTION_DIM,
        action_horizon=ACTION_HORIZON,
    )
    factory = make_arx5_data_config(repo_id)
    data_config = factory.create(dataset_home / ".openpi-assets-unused", model)
    raw_dataset = data_loader.create_torch_dataset(data_config, model.action_horizon, model)
    dataset = data_loader.transform_dataset(raw_dataset, data_config, skip_norm_stats=True)
    sample = dataset[0]
    state = np.asarray(sample["state"])
    actions = np.asarray(sample["actions"])
    if state.shape != (MODEL_ACTION_DIM,):
        raise ValueError(f"openpi padded state must be ({MODEL_ACTION_DIM},), got {state.shape}")
    if actions.shape != (ACTION_HORIZON, MODEL_ACTION_DIM):
        raise ValueError(
            "openpi action chunk must be "
            f"({ACTION_HORIZON}, {MODEL_ACTION_DIM}), got {actions.shape}"
        )
    expected_images = {"base_0_rgb", "left_wrist_0_rgb", "right_wrist_0_rgb"}
    if set(sample["image"]) != expected_images:
        raise ValueError(f"unexpected openpi image keys: {set(sample['image'])}")
    image_shapes = {key: list(np.asarray(value).shape) for key, value in sample["image"].items()}
    if any(tuple(shape) != (224, 224, 3) for shape in image_shapes.values()):
        raise ValueError(f"openpi images must be HWC 224x224 RGB: {image_shapes}")
    if "tokenized_prompt" not in sample or "tokenized_prompt_mask" not in sample:
        raise ValueError("openpi model transform did not tokenize the LeRobot task")
    return {
        "status": "openpi_loader_ready",
        "repo_id": repo_id,
        "dataset_home": str(dataset_home.resolve()),
        "state_shape": list(state.shape),
        "action_shape": list(actions.shape),
        "image_shapes": image_shapes,
        "prompt_tokens": int(np.asarray(sample["tokenized_prompt_mask"]).sum()),
    }


def compute_openpi_norm_stats(
    dataset_home: Path,
    repo_id: str,
    output_dir: Path,
    max_frames: int | None = None,
) -> dict[str, Any]:
    """Compute fresh stats with the same pre-normalization transforms as openpi."""

    import os

    os.environ["HF_LEROBOT_HOME"] = str(dataset_home.resolve())

    import numpy as np
    from openpi.models import pi0_config
    from openpi.shared import normalize
    from openpi.training import data_loader

    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_data_config

    model = pi0_config.Pi0Config(
        pi05=True,
        action_dim=MODEL_ACTION_DIM,
        action_horizon=ACTION_HORIZON,
    )
    data_config = make_arx5_data_config(repo_id).create(output_dir.parent, model)
    raw_dataset = data_loader.create_torch_dataset(data_config, model.action_horizon, model)
    dataset = data_loader.TransformedDataset(
        raw_dataset,
        [*data_config.repack_transforms.inputs, *data_config.data_transforms.inputs],
    )
    limit = len(dataset) if max_frames is None else min(len(dataset), max_frames)
    if limit <= 0:
        raise ValueError("cannot compute norm stats from an empty dataset")
    running = {key: normalize.RunningStats() for key in ("state", "actions")}
    for index in range(limit):
        sample = dataset[index]
        for key, stats in running.items():
            value = np.asarray(sample[key])
            if not np.isfinite(value).all():
                raise ValueError(f"non-finite {key} at dataset index {index}")
            stats.update(value)
    result = {key: stats.get_statistics() for key, stats in running.items()}
    with staged_directory(output_dir) as temporary:
        normalize.save(temporary, result)
    return {
        "status": "fresh_norm_stats_ready",
        "repo_id": repo_id,
        "frames": limit,
        "output_dir": str(output_dir.resolve()),
        "state_dims": int(np.asarray(result["state"].mean).shape[-1]),
        "action_dims": int(np.asarray(result["actions"].mean).shape[-1]),
    }
