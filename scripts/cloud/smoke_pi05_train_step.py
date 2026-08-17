#!/usr/bin/env python3
"""Load pi05_base and run one official JAX/FSDP optimizer step on fake data."""

from __future__ import annotations

import argparse
import dataclasses
import functools
import importlib.util
import json
import math
import os
from pathlib import Path
import time


OPENPI_ROOT = Path(os.environ.get("OPENPI_WORKSPACE", "/workspace/openpi"))
PI05_DATA_ROOT = Path(os.environ.get("PI05_DATA_ROOT", "/mnt/cfs/data/swy/pi05"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-devices", type=int, default=8)
    parser.add_argument("--fsdp-devices", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    return parser.parse_args()


def load_official_train_module():
    path = OPENPI_ROOT / "scripts" / "train.py"
    spec = importlib.util.spec_from_file_location("openpi_official_train", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load official train module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    args = parse_args()

    import jax
    import jax.numpy as jnp

    from openpi.training import config as training_config
    from openpi.training import data_loader
    from openpi.training import sharding
    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_train_config

    if jax.default_backend() != "gpu" or jax.device_count() != args.expected_devices:
        raise RuntimeError(
            f"expected {args.expected_devices} GPU devices, "
            f"got backend={jax.default_backend()} count={jax.device_count()}"
        )

    fsdp_devices = args.fsdp_devices or args.expected_devices

    cache_dir = os.environ.get("JAX_COMPILATION_CACHE_DIR")
    if cache_dir:
        jax.config.update("jax_compilation_cache_dir", cache_dir)

    config = make_arx5_train_config(
        "swy/arx5-synthetic-smoke",
        assets_base_dir=str(PI05_DATA_ROOT / "models" / "assets"),
        checkpoint_base_dir=str(PI05_DATA_ROOT / "checkpoints"),
        batch_size=args.batch_size,
        fsdp_devices=fsdp_devices,
    )
    config = dataclasses.replace(
        config,
        exp_name="synthetic-smoke",
        data=training_config.FakeDataConfig(),
        num_workers=0,
        wandb_enabled=False,
    )

    official_train = load_official_train_module()
    mesh = sharding.make_mesh(config.fsdp_devices)
    data_sharding = jax.sharding.NamedSharding(
        mesh, jax.sharding.PartitionSpec(sharding.DATA_AXIS)
    )
    replicated_sharding = jax.sharding.NamedSharding(
        mesh, jax.sharding.PartitionSpec()
    )

    started = time.monotonic()
    loader = data_loader.create_data_loader(
        config,
        sharding=data_sharding,
        shuffle=False,
        num_batches=1,
    )
    batch = next(iter(loader))
    after_batch = time.monotonic()

    rng = jax.random.key(config.seed)
    train_rng, init_rng = jax.random.split(rng)
    train_state, state_sharding = official_train.init_train_state(
        config, init_rng, mesh, resume=False
    )
    jax.block_until_ready(train_state)
    after_init = time.monotonic()

    compiled_step = jax.jit(
        functools.partial(official_train.train_step, config),
        in_shardings=(replicated_sharding, state_sharding, data_sharding),
        out_shardings=(state_sharding, replicated_sharding),
        donate_argnums=(1,),
    )
    with sharding.set_mesh(mesh):
        train_state, info = compiled_step(train_rng, train_state, batch)
    jax.block_until_ready((train_state, info))
    finished = time.monotonic()

    metrics = jax.device_get(jax.tree.map(jnp.mean, info))
    values = {key: float(value) for key, value in metrics.items()}
    if not all(math.isfinite(value) for value in values.values()):
        raise RuntimeError(f"non-finite training metrics: {values}")

    print(
        json.dumps(
            {
                "result": "pass",
                "devices": jax.device_count(),
                "fsdp_devices": config.fsdp_devices,
                "batch_size": config.batch_size,
                "step": int(train_state.step),
                "metrics": values,
                "seconds": {
                    "data_batch": round(after_batch - started, 3),
                    "model_init_and_checkpoint_load": round(after_init - after_batch, 3),
                    "compile_and_train_step": round(finished - after_init, 3),
                    "total": round(finished - started, 3),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
