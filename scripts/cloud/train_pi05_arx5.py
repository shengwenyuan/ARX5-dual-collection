#!/usr/bin/env python3
"""Map the ARX5 TOML config to pinned openpi training."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import tomllib


OPENPI_ROOT = Path(os.environ.get("OPENPI_WORKSPACE", "/workspace/openpi"))
PI05_DATA_ROOT = Path(os.environ.get("PI05_DATA_ROOT", "/mnt/cfs/data/swy/pi05"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("smoke", "train"), default="train")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_toml(path: Path) -> dict:
    with path.open("rb") as file:
        config = tomllib.load(file)
    if config["run"]["config_name"] != "pi05_arx5_joint_sft":
        raise ValueError("config_name must be pi05_arx5_joint_sft")
    dataset = config["dataset"]
    if dataset["normalization"] != "fresh" or dataset["action_space"] != "joint":
        raise ValueError("training requires fresh stats and joint actions")
    if not dataset["delta_joint_actions"]:
        raise ValueError("joint actions must use openpi delta transforms")
    expected_model = {
        "model_type": "pi05",
        "base_checkpoint": "gs://openpi-assets/checkpoints/pi05_base/params",
        "finetune": "full",
        "action_dim": 32,
        "action_horizon": 50,
        "max_token_len": 200,
        "discrete_state_input": True,
    }
    if config["model"] != expected_model:
        raise ValueError("π0.5 model contract differs from the frozen joint SFT contract")
    if config["train"]["batch_size"] % config["train"]["fsdp_devices"]:
        raise ValueError("batch_size must be divisible by fsdp_devices")
    return config


def load_official_train():
    path = OPENPI_ROOT / "scripts" / "train.py"
    spec = importlib.util.spec_from_file_location("openpi_official_train", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load official train module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolved_summary(config: dict, mode: str) -> dict:
    train = config["train"]
    steps = config["smoke"]["num_train_steps"] if mode == "smoke" else train["num_train_steps"]
    frames = config["dataset"]["frames"]
    return {
        "mode": mode,
        "repo_id": config["dataset"]["repo_id"],
        "global_batch_size": train["batch_size"],
        "fsdp_devices": train["fsdp_devices"],
        "num_train_steps": steps,
        "steps_per_epoch": frames // train["batch_size"],
        "effective_epochs": round(steps * train["batch_size"] / frames, 3),
        "checkpoint_root": str(PI05_DATA_ROOT / "checkpoints"),
        "norm_stats": str(
            PI05_DATA_ROOT / "models" / "assets" / config["run"]["config_name"]
            / config["dataset"]["repo_id"] / "norm_stats.json"
        ),
    }


def validate_runtime_paths(raw: dict) -> None:
    repo_id = raw["dataset"]["repo_id"]
    dataset_root = Path(os.environ.get("HF_LEROBOT_HOME", PI05_DATA_ROOT / "datasets" / "lerobot")) / repo_id
    norm_stats = PI05_DATA_ROOT / "models" / "assets" / raw["run"]["config_name"] / repo_id / "norm_stats.json"
    if not dataset_root.is_dir():
        raise FileNotFoundError(f"dataset not found: {dataset_root}")
    if not norm_stats.is_file():
        raise FileNotFoundError(f"fresh norm stats not found: {norm_stats}")


def build_config(raw: dict, mode: str):
    from openpi.training import optimizer as training_optimizer
    from arx5_collection.pi05_dataset.openpi_adapter import make_arx5_train_config

    run, train, checkpoint = raw["run"], raw["train"], raw["checkpoint"]
    config = make_arx5_train_config(
        raw["dataset"]["repo_id"],
        assets_base_dir=str(PI05_DATA_ROOT / "models" / "assets"),
        checkpoint_base_dir=str(PI05_DATA_ROOT / "checkpoints"),
        batch_size=train["batch_size"],
        fsdp_devices=train["fsdp_devices"],
    )
    lr = {key: value for key, value in raw["lr_schedule"].items() if key != "type"}
    opt = {key: value for key, value in raw["optimizer"].items() if key != "type"}
    if raw["lr_schedule"]["type"] != "cosine" or raw["optimizer"]["type"] != "adamw":
        raise ValueError("only cosine + AdamW is supported")
    config = dataclasses.replace(
        config,
        exp_name=run["exp_name"], project_name=run["project_name"], seed=run["seed"],
        num_workers=train["num_workers"], num_train_steps=train["num_train_steps"],
        log_interval=train["log_interval"], ema_decay=train["ema_decay"],
        wandb_enabled=train["wandb_enabled"], save_interval=checkpoint["save_interval"],
        keep_period=checkpoint["keep_period"], overwrite=checkpoint["overwrite"], resume=checkpoint["resume"],
        lr_schedule=training_optimizer.CosineDecaySchedule(**lr),
        optimizer=training_optimizer.AdamW(**opt),
    )
    if mode == "smoke":
        smoke = raw["smoke"]
        config = dataclasses.replace(
            config, exp_name=config.exp_name + smoke["exp_name_suffix"],
            num_train_steps=smoke["num_train_steps"], log_interval=smoke["log_interval"],
            save_interval=10**9, keep_period=None, wandb_enabled=smoke["wandb_enabled"],
            overwrite=smoke["overwrite"], resume=False,
        )
    return config


def main() -> None:
    args = parse_args()
    raw = load_toml(args.config)
    print(json.dumps(resolved_summary(raw, args.mode), indent=2))
    if args.dry_run:
        return

    import jax

    validate_runtime_paths(raw)
    config = build_config(raw, args.mode)
    if jax.default_backend() != "gpu" or jax.device_count() != config.fsdp_devices:
        raise RuntimeError(f"expected {config.fsdp_devices} GPUs; got {jax.default_backend()}:{jax.device_count()}")
    official_train = load_official_train()
    if args.mode == "smoke" and not raw["smoke"]["save_checkpoints"]:
        official_train._checkpoints.save_state = lambda *args, **kwargs: None

    cache_dir = os.environ.get("JAX_COMPILATION_CACHE_DIR")
    original_update = jax.config.update
    jax.config.update = lambda name, value: original_update(
        name, cache_dir if name == "jax_compilation_cache_dir" and cache_dir else value
    )
    try:
        official_train.main(config)
    finally:
        jax.config.update = original_update


if __name__ == "__main__":
    main()
