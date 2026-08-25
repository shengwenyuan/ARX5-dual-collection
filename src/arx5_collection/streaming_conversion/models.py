from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
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
