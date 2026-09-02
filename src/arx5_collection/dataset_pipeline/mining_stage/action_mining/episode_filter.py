from __future__ import annotations

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import exclude_episode
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    metadata = require_output(context.metadata, "metadata_check")
    cleaning = require_output(context.cleaning, "alignment_report")

    def operation() -> str | None:
        collection_type = metadata.get("collection_type", "demonstration")
        outcome = cleaning.quality.get("outcome")
        if collection_type == "dagger":
            authority = require_output(context.authority, "dagger_authority")
            if outcome not in {"success", "fail"}:
                return f"outcome_{outcome or 'missing'}"
            if cleaning.quality.get("grade") == "C":
                return "quality_grade_c"
            if not authority.valid:
                return "invalid_authority_timeline"
            if not authority.expert_segments:
                return "no_complete_correction"
            return None
        if outcome != "success":
            return f"outcome_{outcome or 'missing'}"
        if cleaning.quality.get("grade") == "C":
            return "quality_grade_c"
        return None

    reason = timed(unit.type, operation)
    if reason is not None:
        exclude_episode(context, reason)
