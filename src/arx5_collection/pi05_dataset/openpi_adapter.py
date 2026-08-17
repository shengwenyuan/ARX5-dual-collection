from __future__ import annotations

from arx5_collection.pi05_dataset.openpi_contract import ACTION_HORIZON
from arx5_collection.pi05_dataset.openpi_contract import CAMERA_KEYS
from arx5_collection.pi05_dataset.openpi_contract import MODEL_ACTION_DIM


def arx5_repack_structure() -> dict[str, object]:
    return {
        "images": {camera: f"observation.images.{camera}" for camera in CAMERA_KEYS},
        "state": "observation.state",
        "actions": "action",
        "prompt": "prompt",
    }


def make_arx5_data_config(repo_id: str):
    """Build the pinned-openpi data adapter without registering a global TrainConfig."""

    from openpi import transforms
    from openpi.training import config as training_config

    repack = transforms.Group(
        inputs=[
            transforms.RepackTransform(arx5_repack_structure())
        ]
    )
    return training_config.LeRobotAlohaDataConfig(
        repo_id=repo_id,
        adapt_to_pi=False,
        repack_transforms=repack,
        base_config=training_config.DataConfig(prompt_from_task=True),
    )


def make_arx5_train_config(
    repo_id: str,
    *,
    assets_base_dir: str = "./assets",
    checkpoint_base_dir: str = "./checkpoints",
    batch_size: int = 64,
    fsdp_devices: int = 8,
    use_pretrained_arx_stats: bool = False,
):
    """Build the joint-only π0.5 SFT config for the 8-GPU training host."""

    import dataclasses

    from openpi.models import pi0_config
    from openpi.training import config as training_config
    from openpi.training import weight_loaders

    data = make_arx5_data_config(repo_id)
    if use_pretrained_arx_stats:
        data = dataclasses.replace(
            data,
            assets=training_config.AssetsConfig(
                assets_dir="gs://openpi-assets/checkpoints/pi05_base/assets",
                asset_id="arx",
            ),
        )
    return training_config.TrainConfig(
        name="pi05_arx5_joint_sft",
        model=pi0_config.Pi0Config(
            pi05=True,
            action_dim=MODEL_ACTION_DIM,
            action_horizon=ACTION_HORIZON,
        ),
        data=data,
        weight_loader=weight_loaders.CheckpointWeightLoader(
            "gs://openpi-assets/checkpoints/pi05_base/params"
        ),
        assets_base_dir=assets_base_dir,
        checkpoint_base_dir=checkpoint_base_dir,
        batch_size=batch_size,
        fsdp_devices=fsdp_devices,
        num_workers=8,
    )
