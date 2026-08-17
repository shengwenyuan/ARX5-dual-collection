#!/usr/bin/env python3
"""Load base/SFT JAX weights and infer once from a real LeRobot observation."""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path


REPO_ID = "local/stacking_five_paper_cups_pi05_v1"
DEFAULT_DATASET_ROOT = Path(
    "/home/lenovo/swy/ARX5-dual-collection-dev/reports/w3/2026-08-16/lerobot"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--index", type=int, default=0)
    return parser.parse_args()


def uint8_image(value):
    import numpy as np

    image = np.asarray(value)
    if image.dtype != np.uint8:
        image = np.rint(np.clip(image, 0, 1) * 255).astype(np.uint8)
    return image


def main() -> None:
    args = parse_args()

    import jax
    import jax.numpy as jnp
    import numpy as np
    from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
    from openpi.models import model as model_lib
    from openpi.policies import policy_config

    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_train_config

    if jax.default_backend() != "gpu" or jax.device_count() != 1:
        raise RuntimeError(
            f"expected one JAX GPU, got backend={jax.default_backend()} count={jax.device_count()}"
        )

    data_root = Path(os.environ["PI05_DATA_ROOT"])
    config = make_arx5_train_config(
        REPO_ID,
        assets_base_dir=str(data_root / "models/assets"),
        checkpoint_base_dir=str(data_root / "checkpoints"),
        batch_size=1,
        fsdp_devices=1,
    )

    base_params = model_lib.restore_params(Path(os.environ["PI05_BASE_PARAMS"]), dtype=jnp.bfloat16)
    base_model = config.model.load(base_params)
    jax.block_until_ready(base_model)
    del base_model, base_params
    gc.collect()

    dataset = LeRobotDataset(REPO_ID, root=args.dataset_root / REPO_ID, download_videos=False)
    sample = dataset[args.index]
    observation = {
        "state": np.asarray(sample["observation.state"], dtype=np.float32),
        "images": {
            "cam_high": uint8_image(sample["observation.images.cam_high"]),
            "cam_left_wrist": uint8_image(sample["observation.images.cam_left_wrist"]),
            "cam_right_wrist": uint8_image(sample["observation.images.cam_right_wrist"]),
        },
        "prompt": str(sample["task"]),
    }
    policy = policy_config.create_trained_policy(config, Path(os.environ["PI05_SFT_CHECKPOINT"]))
    actions = np.asarray(policy.infer(observation)["actions"])
    if actions.shape != (50, 14) or not np.isfinite(actions).all():
        raise RuntimeError(f"invalid action output: shape={actions.shape}")

    print(
        json.dumps(
            {
                "result": "pass",
                "backend": jax.default_backend(),
                "checkpoint": os.environ["PI05_SFT_CHECKPOINT"],
                "dataset_index": args.index,
                "task": observation["prompt"],
                "action_shape": list(actions.shape),
                "action_min": float(actions.min()),
                "action_max": float(actions.max()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
