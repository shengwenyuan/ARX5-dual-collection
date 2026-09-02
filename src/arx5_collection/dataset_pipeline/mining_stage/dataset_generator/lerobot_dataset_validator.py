from __future__ import annotations

from typing import TYPE_CHECKING

from arx5_collection.dataset_pipeline.persistence.artifacts import write_json
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.utils import (
    validate_lerobot,
)

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec


if TYPE_CHECKING:
    from .lerobot_dataset_merge import DatasetGeneratorContext


def run(context: DatasetGeneratorContext, unit: UnitSpec) -> None:
    if context.tasks is None or context.build is None:
        raise RuntimeError("lerobot_dataset_validator requires lerobot_dataset_merge")
    validation = validate_lerobot(
        context.temporary,
        context.repo_id,
        action_horizon=context.recipe.selection.action_horizon,
        expected_task=context.tasks[0] if len(context.tasks) == 1 else None,
    )
    validation["dataset_root"] = str(context.output_path.resolve())
    write_json(
        context.temporary / "reports" / "validation.json",
        validation,
    )
    context.validation = validation
