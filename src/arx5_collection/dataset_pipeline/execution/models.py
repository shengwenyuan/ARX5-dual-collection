from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileIdentity:
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class EpisodeCandidate:
    source_dir: Path
    relative_dir: Path
    include_path: Path
    episode_id: str
    source_session_id: str
    collection_type: str
    outcome: str
    task_id: str
    task_description: str
    mcap: FileIdentity
    metadata: FileIdentity


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    source_root: Path
    include_roots: tuple[Path, ...]
    candidates: tuple[EpisodeCandidate, ...]
    blocked_dirs: tuple[Path, ...]

    @property
    def total_mcap_bytes(self) -> int:
        return sum(candidate.mcap.size for candidate in self.candidates)

    def outcome_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.outcome for item in self.candidates).items()))

    def collection_type_counts(self) -> dict[str, int]:
        return dict(
            sorted(Counter(item.collection_type for item in self.candidates).items())
        )

    def task_counts(self) -> dict[str, int]:
        return dict(
            sorted(Counter(item.task_description for item in self.candidates).items())
        )


class JobState(str, Enum):
    DISCOVERED = "discovered"
    STAGING = "staging"
    CONVERTING = "converting"
    VALIDATING = "validating"
    COMMITTED = "committed"
    EXCLUDED = "excluded"
    DISCARDED = "discarded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunDefinition:
    run_id: str
    source_root: Path
    streaming_root: Path
    output_path: Path
    repo_id: str
    workers: int | None
    pfs_root: Path | None
    stage_workers: int | None
    conversion_workers: int | None
    prefetch_target_bytes: int | None
    prefetch_max_bytes: int | None
    prefetch_max_episodes: int | None
    ready_low_bytes: int | None
    ready_high_bytes: int | None
    temporary_hard_max_bytes: int | None
    max_staged_episodes: int | None
    min_free_bytes: int | None
    recipe_name: str
    recipe_profile: str
    recipe_task: str
    source_materialization: str = "copy"


@dataclass(frozen=True, slots=True)
class SelectionEntry:
    episode_id: str
    source_session_id: str
    source_dir: Path
    relative_dir: Path
    collection_type: str
    outcome: str
    metadata_task_id: str
    metadata_task_description: str
    training_task: str
    mcap: FileIdentity
    metadata: FileIdentity


@dataclass(frozen=True, slots=True)
class JobEvent:
    event_index: int
    episode_id: str
    previous_state: JobState | None
    state: JobState
    attempt: int
    recorded_at: str
    reason_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    episode_id: str
    state: JobState
    attempt: int
    event_index: int
    reason_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StageReceipt:
    episode_id: str
    source_session_id: str
    source_dir: Path
    stage_dir: Path
    mcap: FileIdentity
    metadata: FileIdentity
    materialization: str = "copy"


class ConversionStatus(str, Enum):
    COMMITTED = "committed"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class EpisodeConversionResult:
    episode_id: str
    status: ConversionStatus
    fragment_dir: Path | None
    segment_count: int
    frame_count: int
    reason_code: str | None = None
    phase_seconds: tuple[tuple[str, float], ...] = ()
