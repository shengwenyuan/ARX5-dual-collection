from __future__ import annotations

import json
import os
from pathlib import Path

from arx5_collection.dataset_pipeline.configuration.run import SourceConfig
from arx5_collection.dataset_pipeline.execution.models import DiscoveryResult
from arx5_collection.dataset_pipeline.execution.models import EpisodeCandidate
from arx5_collection.dataset_pipeline.execution.models import FileIdentity


def discover_episodes(config: SourceConfig) -> DiscoveryResult:
    source_root = config.root.resolve(strict=True)
    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    include_roots = tuple(
        _resolve_inside(source_root / relative, source_root)
        for relative in config.include_paths
    )
    _reject_overlapping_roots(include_roots)

    candidates: list[EpisodeCandidate] = []
    blocked_dirs: list[Path] = []
    visited: set[Path] = set()
    for include_path, include_root in zip(config.include_paths, include_roots):
        _walk(
            include_root,
            include_path,
            source_root,
            frozenset(config.block),
            visited,
            candidates,
            blocked_dirs,
        )

    by_id: dict[str, Path] = {}
    for candidate in candidates:
        if previous := by_id.get(candidate.episode_id):
            raise ValueError(
                f"duplicate episode id {candidate.episode_id!r}: "
                f"{previous} and {candidate.source_dir}"
            )
        by_id[candidate.episode_id] = candidate.source_dir
    ordered = tuple(sorted(candidates, key=lambda item: item.episode_id))
    return DiscoveryResult(
        source_root=source_root,
        include_roots=include_roots,
        candidates=ordered,
        blocked_dirs=tuple(sorted(blocked_dirs, key=str)),
    )


def _walk(
    directory: Path,
    include_path: Path,
    source_root: Path,
    block: frozenset[str],
    visited: set[Path],
    candidates: list[EpisodeCandidate],
    blocked_dirs: list[Path],
) -> None:
    directory = _resolve_inside(directory, source_root)
    if directory in visited:
        raise ValueError(
            f"source directory is reachable through multiple paths: {directory}"
        )
    visited.add(directory)

    mcap_path = directory / "episode.mcap"
    metadata_path = directory / "metadata.json"
    if mcap_path.is_file() and metadata_path.is_file():
        candidates.append(
            _candidate(directory, include_path, source_root, mcap_path, metadata_path)
        )
        return

    with os.scandir(directory) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for entry in children:
        if not entry.is_dir(follow_symlinks=True):
            continue
        child = Path(entry.path)
        if entry.name in block:
            blocked_dirs.append(child.relative_to(source_root))
            continue
        _walk(
            child,
            include_path,
            source_root,
            block,
            visited,
            candidates,
            blocked_dirs,
        )


def _candidate(
    directory: Path,
    include_path: Path,
    source_root: Path,
    mcap_path: Path,
    metadata_path: Path,
) -> EpisodeCandidate:
    mcap_path = _resolve_inside(mcap_path, source_root)
    metadata_path = _resolve_inside(metadata_path, source_root)
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"invalid Episode metadata: {metadata_path}: {error}"
        ) from error
    if not isinstance(metadata, dict):
        raise ValueError(f"Episode metadata must be an object: {metadata_path}")
    episode_id = _metadata_string(metadata, "episode_id", metadata_path)
    if directory.name != episode_id:
        raise ValueError(
            f"Episode directory name {directory.name!r} does not match metadata "
            f"episode_id {episode_id!r}"
        )
    task = metadata.get("task")
    if not isinstance(task, dict):
        raise ValueError(f"Episode metadata task must be an object: {metadata_path}")
    task_id = _metadata_string(task, "id", metadata_path)
    task_description = _metadata_string(task, "description", metadata_path)
    relative_dir = directory.relative_to(source_root)
    collection_type = metadata.get("collection_type", "demonstration")
    if not isinstance(collection_type, str) or not collection_type:
        raise ValueError(f"invalid collection_type in {metadata_path}")
    outcome = _metadata_string(metadata, "outcome", metadata_path)
    mcap_stat = mcap_path.stat()
    metadata_stat = metadata_path.stat()
    return EpisodeCandidate(
        source_dir=directory,
        relative_dir=relative_dir,
        include_path=include_path,
        episode_id=episode_id,
        source_session_id=_source_session_id(metadata, relative_dir),
        collection_type=collection_type,
        outcome=outcome,
        task_id=task_id,
        task_description=task_description,
        mcap=FileIdentity(mcap_stat.st_size, mcap_stat.st_mtime_ns),
        metadata=FileIdentity(metadata_stat.st_size, metadata_stat.st_mtime_ns),
    )


def _source_session_id(metadata: dict[str, object], relative_dir: Path) -> str:
    station = metadata.get("station")
    timing = metadata.get("timing")
    station_id = station.get("id") if isinstance(station, dict) else None
    started_at = timing.get("started_at") if isinstance(timing, dict) else None
    day = (
        started_at[:10]
        if isinstance(started_at, str) and len(started_at) >= 10
        else None
    )
    parent = relative_dir.parent.as_posix()
    parts = [station_id, day, parent]
    return "/".join(str(part) for part in parts if part)


def _metadata_string(payload: dict[str, object], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Episode metadata {key} must be a non-empty string: {path}")
    return value


def _resolve_inside(path: Path, source_root: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(source_root)
    except ValueError as error:
        raise ValueError(
            f"source path escapes configured root: {path} -> {resolved}"
        ) from error
    return resolved


def _reject_overlapping_roots(roots: tuple[Path, ...]) -> None:
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError(f"source include paths overlap: {left} and {right}")
