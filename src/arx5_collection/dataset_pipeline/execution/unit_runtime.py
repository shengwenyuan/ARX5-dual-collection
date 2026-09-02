from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import TypeVar

from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.models import (
    PairingResult,
)
from arx5_collection.dataset_pipeline.source.models import CleaningResult
from arx5_collection.dataset_pipeline.source.models import EpisodeSanitycheckResult
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    AuthorityClassification,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    DatasetSelection,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    Pi05Sample,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    Pi05Segment,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    SegmentProvenance,
)

from arx5_collection.dataset_pipeline.configuration.recipe import DatasetPipelineRecipe

from .models import StageReceipt


T = TypeVar("T")
TimedRunner = Callable[[str, Callable[[], T]], T]


@dataclass(frozen=True, slots=True)
class ActionMiningInterval:
    scan: EpisodeScan
    frame_groups: tuple[FrameGroup, ...]
    provenance: SegmentProvenance | None


@dataclass(slots=True)
class EpisodePipelineContext:
    receipt: StageReceipt
    output_root: Path
    task: str
    repo_id: str
    recipe: DatasetPipelineRecipe
    metadata: dict[str, Any] | None = None
    scan: EpisodeScan | None = None
    timeline: dict[str, dict[str, int | None]] | None = None
    timeline_has_errors: bool = False
    excessive_gap_topics: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    sanitycheck: EpisodeSanitycheckResult | None = None
    pairing: PairingResult | None = None
    cleaning: CleaningResult | None = None
    authority: AuthorityClassification | None = None
    mining_intervals: tuple[ActionMiningInterval, ...] | None = None
    interval_samples: tuple[tuple[Pi05Sample, ...], ...] | None = None
    mined_samples: tuple[Pi05Sample, ...] | None = None
    mined_segments: tuple[Pi05Segment, ...] | None = None
    segment_provenance: tuple[SegmentProvenance, ...] = ()
    selection: DatasetSelection | None = None
    dataset_root: Path | None = None
    validation: dict[str, Any] | None = None
    exclusion_reason: str | None = None
    reported_phase_seconds: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EpisodePipelineResult:
    metadata: dict[str, Any]
    cleaning: CleaningResult
    selection: DatasetSelection
    dataset_root: Path | None
    validation: dict[str, Any] | None
    exclusion_reason: str | None
    phase_seconds: tuple[tuple[str, float], ...]


def require_output(value: T | None, unit_type: str) -> T:
    if value is None:
        raise RuntimeError(f"unit requires output from {unit_type}")
    return value


def exclude_episode(context: EpisodePipelineContext, reason: str) -> None:
    context.selection = DatasetSelection(
        (),
        ({"episode_id": context.receipt.episode_id, "reason": reason},),
    )
    context.exclusion_reason = reason
