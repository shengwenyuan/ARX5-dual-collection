from __future__ import annotations

import time
from pathlib import Path
from typing import Callable
from typing import TypeVar

from arx5_collection.dataset_pipeline.configuration.recipe import DatasetPipelineRecipe
from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.mining_stage.action_mining.registry import (
    UNIT_RUNNERS as ACTION_MINING_UNIT_RUNNERS,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.registry import (
    DATASET_UNIT_RUNNERS,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.registry import (
    EPISODE_UNIT_RUNNERS as DATASET_EPISODE_UNIT_RUNNERS,
)
from arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.registry import (
    UNIT_RUNNERS as EPISODE_SANITYCHECK_UNIT_RUNNERS,
)

from .models import StageReceipt
from .unit_runtime import EpisodePipelineContext
from .unit_runtime import EpisodePipelineResult


T = TypeVar("T")
UnitRunner = Callable[
    [EpisodePipelineContext, UnitSpec, Callable[[str, Callable[[], T]], T]],
    None,
]


_DATASET_UNITS = set(DATASET_UNIT_RUNNERS)
EPISODE_UNIT_RUNNERS: dict[str, UnitRunner] = {
    **EPISODE_SANITYCHECK_UNIT_RUNNERS,
    **ACTION_MINING_UNIT_RUNNERS,
    **DATASET_EPISODE_UNIT_RUNNERS,
}


def run_episode_pipeline(
    receipt: StageReceipt,
    output_root: Path,
    recipe: DatasetPipelineRecipe,
    task: str,
    repo_id: str,
) -> EpisodePipelineResult:
    context = EpisodePipelineContext(receipt, output_root, task, repo_id, recipe)
    phases: list[tuple[str, float]] = []

    def timed(name: str, operation: Callable[[], T]) -> T:
        started = time.monotonic()
        try:
            return operation()
        finally:
            phases.append((name, max(time.monotonic() - started, 0.0)))

    for stage in recipe.pipeline.stages:
        for unit in stage.units:
            if unit.type in _DATASET_UNITS:
                continue
            runner = EPISODE_UNIT_RUNNERS[unit.type]
            runner(context, unit, timed)
            if context.exclusion_reason is not None:
                return _result(context, phases)
    return _result(context, phases)


def _result(
    context: EpisodePipelineContext,
    phases: list[tuple[str, float]],
) -> EpisodePipelineResult:
    if context.metadata is None:
        raise RuntimeError("Episode pipeline did not produce metadata")
    if context.cleaning is None:
        raise RuntimeError("Episode pipeline did not produce alignment artifacts")
    if context.selection is None:
        raise RuntimeError("Episode pipeline did not produce action mining artifacts")
    if context.exclusion_reason is None and (
        context.dataset_root is None or context.validation is None
    ):
        raise RuntimeError(
            "Episode pipeline did not produce a validated Dataset fragment"
        )
    return EpisodePipelineResult(
        metadata=context.metadata,
        cleaning=context.cleaning,
        selection=context.selection,
        dataset_root=context.dataset_root,
        validation=context.validation,
        exclusion_reason=context.exclusion_reason,
        phase_seconds=tuple(phases + context.reported_phase_seconds),
    )
