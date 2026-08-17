#!/usr/bin/env python3
"""Fail-fast validation for the pinned openpi JAX training environment."""

from __future__ import annotations

import argparse
import functools
from importlib import metadata
import json
import platform


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-devices", type=int, default=8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    import jax
    import jax.numpy as jnp
    from jax import lax
    import torch

    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_train_config

    nccl_package_version = metadata.version("nvidia-nccl-cu12")
    if nccl_package_version != "2.26.5":
        raise RuntimeError(
            f"nvidia-nccl-cu12 is {nccl_package_version}, expected host fix 2.26.5"
        )

    devices = jax.devices()
    if jax.default_backend() != "gpu":
        raise RuntimeError(f"JAX backend is {jax.default_backend()!r}, expected 'gpu'")
    if len(devices) != args.expected_devices:
        raise RuntimeError(f"JAX found {len(devices)} devices, expected {args.expected_devices}")

    # One matmul per device plus a cross-device sum exercises GPU kernels and NCCL.
    inputs = jnp.ones((len(devices), 1024, 1024), dtype=jnp.float32)

    @functools.partial(jax.pmap, axis_name="devices")
    def distributed_smoke(x):
        local_mean = jnp.mean(x @ x.T)
        device_sum = lax.psum(jnp.array(1, dtype=jnp.int32), "devices")
        return local_mean, device_sum

    local_means, device_sums = jax.block_until_ready(distributed_smoke(inputs))
    if not bool(jnp.all(device_sums == len(devices))):
        raise RuntimeError(f"cross-device sum failed: {device_sums}")
    if not bool(jnp.all(jnp.isfinite(local_means))):
        raise RuntimeError("distributed matmul returned non-finite values")

    config = make_arx5_train_config(
        "swy/arx5-placeholder",
        assets_base_dir="/mnt/cfs/data/swy/pi05/models/assets",
        checkpoint_base_dir="/mnt/cfs/data/swy/pi05/checkpoints",
    )
    if config.model.action_dim != 32 or config.model.action_horizon != 50:
        raise RuntimeError("ARX5 π0.5 model dimensions drifted from 32D/50-step")
    if config.fsdp_devices != args.expected_devices:
        raise RuntimeError("ARX5 training config is not configured for 8-way FSDP")

    result = {
        "host": platform.node(),
        "jax_version": jax.__version__,
        "jax_backend": jax.default_backend(),
        "jax_device_count": len(devices),
        "jax_devices": [str(device) for device in devices],
        "collective_sum": int(device_sums[0]),
        "torch_version": torch.__version__,
        "torch_cuda_available": torch.cuda.is_available(),
        "nvidia_nccl_cu12": nccl_package_version,
        "train_config": {
            "name": config.name,
            "action_dim": config.model.action_dim,
            "action_horizon": config.model.action_horizon,
            "batch_size": config.batch_size,
            "fsdp_devices": config.fsdp_devices,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
