#!/usr/bin/env python3
"""Verify the isolated π0.5 JAX runtime deployed on w3."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


EXPECTED_OPENPI_COMMIT = "15a9616a00943ada6c20a0f158e3adb39df2ccac"


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-cpu", action="store_true")
    args = parser.parse_args()

    openpi_root = Path(os.environ["OPENPI_WORKSPACE"])
    data_root = Path(os.environ["PI05_DATA_ROOT"])
    checkpoint_root = data_root / "checkpoints/pi05_arx5_joint_sft/stacking_five_paper_cups_pi05_v1"
    commit = subprocess.check_output(
        ["git", "-C", openpi_root, "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != EXPECTED_OPENPI_COMMIT:
        raise RuntimeError(f"unexpected openpi commit: {commit}")

    required = [
        data_root / "models/big_vision/paligemma_tokenizer.model",
        data_root / "models/openpi-assets/checkpoints/pi05_base/params/commit_success.txt",
        data_root / "models/assets/pi05_arx5_joint_sft/local/stacking_five_paper_cups_pi05_v1/norm_stats.json",
    ]
    for step in ("5000", "9999"):
        required.extend(
            [
                checkpoint_root / step / "params/manifest.ocdbt",
                checkpoint_root / step / "assets/local/stacking_five_paper_cups_pi05_v1/norm_stats.json",
            ]
        )
    for path in required:
        require_file(path)

    import jax
    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_train_config

    config = make_arx5_train_config(
        "local/stacking_five_paper_cups_pi05_v1",
        assets_base_dir=str(data_root / "models/assets"),
        checkpoint_base_dir=str(data_root / "checkpoints"),
        batch_size=1,
        fsdp_devices=1,
    )
    backend = jax.default_backend()
    if not args.allow_cpu and backend != "gpu":
        raise RuntimeError(f"expected JAX GPU backend, got {backend}")
    if config.model.action_dim != 32 or config.model.action_horizon != 50:
        raise RuntimeError("unexpected π0.5 action contract")

    print(
        json.dumps(
            {
                "status": "ready" if backend == "gpu" else "files-ready-gpu-blocked",
                "python": sys.version.split()[0],
                "openpi_commit": commit,
                "jax": jax.__version__,
                "backend": backend,
                "devices": [str(device) for device in jax.devices()],
                "action_dim": config.model.action_dim,
                "action_horizon": config.model.action_horizon,
                "checkpoints": ["5000", "9999"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
