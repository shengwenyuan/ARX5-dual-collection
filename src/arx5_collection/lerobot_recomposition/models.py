from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class OutputConfig:
    backend: str
    repo_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    path: Path
    select_all: bool
    selection_manifest: Path | None


@dataclass(frozen=True, slots=True)
class V3RuntimeConfig:
    python: Path


@dataclass(frozen=True, slots=True)
class CompositionConfig:
    schema_version: int
    output: OutputConfig
    sources: tuple[SourceConfig, ...]
    v3_runtime: V3RuntimeConfig | None
    config_path: Path


@dataclass(frozen=True, slots=True)
class EpisodeDescriptor:
    episode_index: int
    length: int
    tasks: tuple[str, ...]
    provenance: dict[str, Any]
    physical: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SnapshotDescriptor:
    name: str
    root: Path
    repo_id: str
    backend: str
    metadata_fingerprint: str
    info: dict[str, Any]
    snapshot: dict[str, Any]
    episodes: tuple[EpisodeDescriptor, ...]
    tasks: dict[int, str]
    episode_stats: dict[int, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SelectedEpisode:
    source: SnapshotDescriptor
    episode: EpisodeDescriptor


@dataclass(frozen=True, slots=True)
class CompositionPlan:
    config: CompositionConfig
    selected: tuple[SelectedEpisode, ...]
    tasks: tuple[str, ...]
    frame_count: int
    video_count: int
    contract: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CompositionResult:
    output_path: Path
    repo_id: str
    backend: str
    episode_count: int
    frame_count: int
    video_count: int
    tasks: tuple[str, ...]
    plan_fingerprint: str
