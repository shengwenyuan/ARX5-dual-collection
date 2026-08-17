#!/usr/bin/env python3
"""Compute fresh ARX5 statistics with openpi's pinned implementation."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys


OPENPI_ROOT = Path(os.environ.get("OPENPI_WORKSPACE", "/workspace/openpi"))
PI05_DATA_ROOT = Path(os.environ.get("PI05_DATA_ROOT", "/mnt/cfs/data/swy/pi05"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--num-workers", type=int, default=8)
    parser.add_argument("--max-frames", type=int)
    return parser.parse_args()


def load_official_norm_module():
    scripts_dir = OPENPI_ROOT / "scripts"
    sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("compute_norm_stats")


def main() -> None:
    args = parse_args()

    import numpy as np
    import tqdm

    import openpi.shared.normalize as normalize
    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_train_config

    config = make_arx5_train_config(
        args.repo_id,
        assets_base_dir=str(PI05_DATA_ROOT / "models" / "assets"),
        checkpoint_base_dir=str(PI05_DATA_ROOT / "checkpoints"),
        batch_size=args.batch_size,
    )
    data_config = config.data.create(config.assets_dirs, config.model)
    official_norm = load_official_norm_module()
    loader, num_batches = official_norm.create_torch_dataloader(
        data_config,
        config.model.action_horizon,
        args.batch_size,
        config.model,
        args.num_workers,
        args.max_frames,
    )
    if num_batches < 1:
        raise RuntimeError("dataset is smaller than one norm-stats batch")

    running = {key: normalize.RunningStats() for key in ("state", "actions")}
    for batch in tqdm.tqdm(loader, total=num_batches, desc="Computing stats"):
        for key, stats in running.items():
            stats.update(np.asarray(batch[key]))

    norm_stats = {key: stats.get_statistics() for key, stats in running.items()}
    output_path = config.assets_dirs / data_config.repo_id
    print(f"Writing stats to: {output_path}")
    normalize.save(output_path, norm_stats)


if __name__ == "__main__":
    main()
