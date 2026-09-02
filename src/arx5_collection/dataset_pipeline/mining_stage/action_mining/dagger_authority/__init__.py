from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from arx5_collection.dataset_pipeline.source.reader import load_metadata
from .artifacts import write_authority_artifacts
from .classifier import classify_authority
from .classifier import AuthorityAlignmentPolicy
from ..models import AuthorityClassification
from ..models import AuthorityEventRecord
from .reader import read_authority_events

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


EventReader = Callable[[Path], tuple[AuthorityEventRecord, ...]]


def classify_dagger_episode(
    episode_dir: Path,
    audit_root: Path,
    policy: AuthorityAlignmentPolicy,
    *,
    event_reader: EventReader = read_authority_events,
) -> tuple[AuthorityClassification, Path]:
    metadata = load_metadata(episode_dir)
    try:
        events = event_reader(episode_dir)
    except (KeyError, TypeError, ValueError) as error:
        dagger_metadata = metadata.get("dagger")
        intervention_count = (
            int(dagger_metadata.get("intervention_count", 0))
            if isinstance(dagger_metadata, dict)
            else 0
        )
        result = AuthorityClassification(
            episode_id=str(metadata.get("episode_id", episode_dir.name)),
            valid=False,
            issues=(f"authority reader failed: {error}",),
            episode_monotonic_anchor_ns=None,
            episode_bag_anchor_ns=None,
            bag_anchor_spread_ns=None,
            event_count=0,
            intervention_count=intervention_count,
            segments=(),
        )
    else:
        result = classify_authority(metadata, events, policy)
    output = write_authority_artifacts(audit_root, result)
    return result, output


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    metadata = require_output(context.metadata, "metadata_check")
    if metadata.get("collection_type", "demonstration") == "dagger":
        policy = AuthorityAlignmentPolicy.from_params(unit.params)
        context.authority, _ = timed(
            unit.type,
            lambda: classify_dagger_episode(
                context.receipt.stage_dir,
                context.output_root / "audit",
                policy,
            ),
        )
