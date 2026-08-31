from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from arx5_collection.artifacts import read_jsonl

from .models import CompositionConfig
from .models import CompositionPlan
from .models import EpisodeDescriptor
from .models import SelectedEpisode
from .models import SnapshotDescriptor
from .v21 import read_v21_snapshot


_INFO_CONTRACT_KEYS = (
    "codebase_version",
    "robot_type",
    "fps",
    "features",
)


def build_plan(config: CompositionConfig) -> CompositionPlan:
    descriptors = tuple(
        _read_source(config, source.name, source.path) for source in config.sources
    )
    selected = []
    for source_config, descriptor in zip(config.sources, descriptors, strict=True):
        episodes = descriptor.episodes if source_config.select_all else _select_manifest(
            descriptor, source_config.selection_manifest
        )
        selected.extend(SelectedEpisode(descriptor, episode) for episode in episodes)
    if not selected:
        raise ValueError("composition selects no Episodes")
    selected_tuple = tuple(selected)
    contract = _compatible_contract(selected_tuple)
    _validate_unique_segments(selected_tuple)
    _validate_task_scope(selected_tuple)
    _validate_v3_shard_selection(selected_tuple)
    tasks = _ordered_tasks(selected_tuple)
    frames = sum(item.episode.length for item in selected_tuple)
    video_keys = _video_keys(contract["info"])
    videos = len(selected_tuple) * len(video_keys)
    fingerprint = _plan_fingerprint(config, selected_tuple, contract)
    return CompositionPlan(config, selected_tuple, tasks, frames, videos, contract, fingerprint)


def _read_source(config: CompositionConfig, name: str, root: Path) -> SnapshotDescriptor:
    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        raise ValueError(f"source info.json is missing: {root}")
    info = json.loads(info_path.read_text())
    version = info.get("codebase_version")
    if version == "v2.1":
        return read_v21_snapshot(name, root)
    if version == "v3.0":
        if config.v3_runtime is None:
            raise ValueError("v3_runtime.python is required for a v3.0 source")
        from .v3 import V3WorkerClient

        return V3WorkerClient(config.v3_runtime.python).inspect(name, root)
    raise ValueError(f"unsupported source LeRobot version {version!r}: {root}")


def _select_manifest(
    descriptor: SnapshotDescriptor,
    manifest_path: Path | None,
) -> tuple[EpisodeDescriptor, ...]:
    if manifest_path is None or not manifest_path.is_file():
        raise ValueError(f"selection manifest is missing: {manifest_path}")
    by_segment = {
        _string(episode.provenance.get("segment_id"), "segment_id"): episode
        for episode in descriptor.episodes
    }
    result = []
    seen: set[str] = set()
    for row in read_jsonl(manifest_path):
        required = {
            "segment_id",
            "source_episode_id",
            "source_session_id",
            "expected_lerobot_episode_index",
        }
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(f"selection manifest row is incomplete: {missing}")
        segment_id = _string(row.get("segment_id"), "selection segment_id")
        if segment_id in seen:
            raise ValueError(f"duplicate segment in selection manifest: {segment_id}")
        seen.add(segment_id)
        try:
            episode = by_segment[segment_id]
        except KeyError as error:
            raise ValueError(f"selection segment is absent from source: {segment_id}") from error
        expected = (
            row["source_episode_id"],
            row["source_session_id"],
            row["expected_lerobot_episode_index"],
        )
        observed = (
            episode.provenance.get("source_episode_id"),
            episode.provenance.get("source_session_id"),
            episode.episode_index,
        )
        if expected != observed:
            raise ValueError(f"selection identity drift for segment: {segment_id}")
        result.append(episode)
    if not result:
        raise ValueError(f"selection manifest is empty: {manifest_path}")
    return tuple(result)


def _compatible_contract(selected: tuple[SelectedEpisode, ...]) -> dict[str, Any]:
    baseline = selected[0].source
    baseline_info = _info_contract(baseline.info, normalize_version=True)
    baseline_recipe = baseline.snapshot.get("recipe")
    if not isinstance(baseline_recipe, dict) or not baseline_recipe:
        raise ValueError(f"source snapshot has no frozen recipe: {baseline.root}")
    for item in selected[1:]:
        if _info_contract(item.source.info, normalize_version=True) != baseline_info:
            raise ValueError(f"LeRobot training contract drift: {item.source.name}")
        if item.source.snapshot.get("recipe") != baseline_recipe:
            raise ValueError(f"snapshot recipe drift: {item.source.name}")
    return {"info": baseline_info, "recipe": baseline_recipe}


def _info_contract(info: dict[str, Any], *, normalize_version: bool) -> dict[str, Any]:
    missing = [key for key in _INFO_CONTRACT_KEYS if key not in info]
    if missing:
        raise ValueError(f"LeRobot info contract is incomplete: {missing}")
    result = {key: info[key] for key in _INFO_CONTRACT_KEYS}
    if normalize_version:
        result["codebase_version"] = "logical-v2.1-or-v3.0"
        result["features"] = _logical_features(result["features"])
    return result


def _logical_features(value: object) -> object:
    if not isinstance(value, dict):
        return value
    result = {}
    for name, feature in value.items():
        if not isinstance(feature, dict):
            result[name] = feature
            continue
        result[name] = {
            key: feature[key]
            for key in ("dtype", "shape", "names")
            if key in feature
        }
    return result


def _validate_unique_segments(selected: tuple[SelectedEpisode, ...]) -> None:
    seen = set()
    for item in selected:
        segment = _string(item.episode.provenance.get("segment_id"), "segment_id")
        if segment in seen:
            raise ValueError(f"duplicate segment across composition sources: {segment}")
        seen.add(segment)


def _validate_task_scope(selected: tuple[SelectedEpisode, ...]) -> None:
    episode_tasks: dict[str, set[str]] = {}
    for item in selected:
        episode_id = _string(item.episode.provenance.get("source_episode_id"), "source_episode_id")
        episode_tasks.setdefault(episode_id, set()).update(item.episode.tasks)
    for episode_id, tasks in episode_tasks.items():
        if len(tasks) != 1:
            raise ValueError(f"task mismatch within source Episode: {episode_id}")


def _validate_v3_shard_selection(selected: tuple[SelectedEpisode, ...]) -> None:
    sources: dict[str, SnapshotDescriptor] = {}
    chosen: dict[str, set[int]] = {}
    ordered: dict[str, list[int]] = {}
    for item in selected:
        sources[item.source.name] = item.source
        chosen.setdefault(item.source.name, set()).add(item.episode.episode_index)
        ordered.setdefault(item.source.name, []).append(item.episode.episode_index)
    for name, source in sources.items():
        if source.backend != "lerobot-v3.0" or len(chosen[name]) == len(source.episodes):
            if source.backend != "lerobot-v3.0":
                continue
        shard_members: dict[tuple[str, int, int], set[int]] = {}
        for episode in source.episodes:
            videos = episode.physical.get("video_shards")
            if not isinstance(videos, dict):
                raise ValueError(f"v3 source has no video shard map: {source.root}")
            for key, pair in videos.items():
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError(f"v3 video shard identity is invalid: {source.root}")
                shard_members.setdefault((key, int(pair[0]), int(pair[1])), set()).add(
                    episode.episode_index
                )
        for shard, members in shard_members.items():
            intersection = members & chosen[name]
            if intersection and intersection != members:
                raise ValueError(
                    f"v3 selection cuts shared video shard {shard} in source {name}; re-encoding is forbidden"
                )
        _validate_v3_component_order(source, ordered[name], shard_members)


def _validate_v3_component_order(
    source: SnapshotDescriptor,
    desired: list[int],
    shard_members: dict[tuple[str, int, int], set[int]],
) -> None:
    parent = {episode.episode_index: episode.episode_index for episode in source.episodes}

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for members in shard_members.values():
        ordered = sorted(members)
        for value in ordered[1:]:
            union(ordered[0], value)
    components: dict[int, list[int]] = {}
    for index in parent:
        components.setdefault(find(index), []).append(index)
    # Source descriptors retain local order. A shared shard component cannot be
    # internally reordered without rewriting its video timeline.
    cursor = 0
    while cursor < len(desired):
        component = sorted(components[find(desired[cursor])])
        if desired[cursor : cursor + len(component)] != component:
            raise ValueError(
                f"v3 selection reorders Episodes within a shared video-shard component in source {source.name}"
            )
        cursor += len(component)


def _ordered_tasks(selected: tuple[SelectedEpisode, ...]) -> tuple[str, ...]:
    tasks = []
    for item in selected:
        for task in item.episode.tasks:
            if task not in tasks:
                tasks.append(task)
    if not tasks:
        raise ValueError("composition contains no task")
    return tuple(tasks)


def _video_keys(info_contract: dict[str, Any]) -> tuple[str, ...]:
    features = info_contract.get("features")
    if not isinstance(features, dict):
        raise ValueError("LeRobot features must be an object")
    return tuple(sorted(
        key for key, feature in features.items()
        if isinstance(feature, dict) and feature.get("dtype") == "video"
    ))


def _plan_fingerprint(
    config: CompositionConfig,
    selected: tuple[SelectedEpisode, ...],
    contract: dict[str, Any],
) -> str:
    payload = {
        "schema_version": config.schema_version,
        "output": {
            "backend": config.output.backend,
            "repo_id": config.output.repo_id,
            "path": str(config.output.path),
        },
        "contract": contract,
        "episodes": [
            {
                "source": item.source.name,
                "source_root": str(item.source.root),
                "source_fingerprint": item.source.metadata_fingerprint,
                "segment_id": item.episode.provenance["segment_id"],
                "source_episode_id": item.episode.provenance["source_episode_id"],
                "source_session_id": item.episode.provenance["source_session_id"],
                "lerobot_episode_index": item.episode.episode_index,
            }
            for item in selected
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value
