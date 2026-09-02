from __future__ import annotations

from arx5_collection.dataset_pipeline.source.reader import read_episode_scan

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
    require_output(context.metadata, "metadata_check")
    context.scan = timed(
        unit.type,
        lambda: read_episode_scan(context.receipt.stage_dir),
    )
