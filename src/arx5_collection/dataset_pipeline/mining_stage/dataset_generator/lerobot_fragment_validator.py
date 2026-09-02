from __future__ import annotations

from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.utils import (
    validate_lerobot,
)

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    dataset_root = require_output(
        context.dataset_root,
        "lerobot_fragment_generator",
    )
    context.validation = timed(
        unit.type,
        lambda: validate_lerobot(
            dataset_root,
            context.repo_id,
            action_horizon=context.recipe.selection.action_horizon,
            expected_task=context.task,
        ),
    )
