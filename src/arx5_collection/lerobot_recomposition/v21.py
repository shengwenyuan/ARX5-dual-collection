from __future__ import annotations

from copy import deepcopy
import errno
import hashlib
import math
import os
from pathlib import Path
import shutil
from typing import Any
from typing import Callable

from arx5_collection.artifacts import read_json
from arx5_collection.artifacts import read_jsonl
from arx5_collection.artifacts import write_json
from arx5_collection.artifacts import write_jsonl
from arx5_collection.pi05_dataset.validate import validate_lerobot

from .atomic import preserved_staging_directory
from .models import CompositionPlan
from .models import CompositionResult
from .models import EpisodeDescriptor
from .models import SnapshotDescriptor


def read_v21_snapshot(name: str, root: Path) -> SnapshotDescriptor:
    snapshot_path = root / "snapshot.json"
    manifest_path = root / "reports" / "source_manifest.jsonl"
    info_path = root / "meta" / "info.json"
    required = (
        snapshot_path,
        manifest_path,
        info_path,
        root / "meta" / "episodes.jsonl",
        root / "meta" / "episodes_stats.jsonl",
        root / "meta" / "tasks.jsonl",
    )
    for path in required:
        _required_file(path)
    snapshot = read_json(snapshot_path)
    info = read_json(info_path)
    if snapshot.get("status") != "committed":
        raise ValueError(f"source snapshot is not committed: {root}")
    if snapshot.get("builder_backend") != "lerobot-v2.1":
        raise ValueError(f"source snapshot backend is not lerobot-v2.1: {root}")
    if info.get("codebase_version") != "v2.1":
        raise ValueError(f"source dataset is not LeRobot v2.1: {root}")
    repo_id = _string(snapshot.get("repo_id"), "snapshot repo_id")

    episode_rows = read_jsonl(root / "meta" / "episodes.jsonl")
    stats = _indexed(read_jsonl(root / "meta" / "episodes_stats.jsonl"), "episode_index", "stats")
    tasks = _task_map(read_jsonl(root / "meta" / "tasks.jsonl"))
    provenance = _indexed(read_jsonl(manifest_path), "lerobot_episode_index", "source manifest")
    expected = set(range(len(episode_rows)))
    episodes_by_index = _indexed(episode_rows, "episode_index", "episodes")
    if set(episodes_by_index) != expected or set(stats) != expected or set(provenance) != expected:
        raise ValueError(f"source Episode metadata is not contiguous and complete: {root}")
    if info.get("total_episodes") != len(expected):
        raise ValueError(f"source info total_episodes mismatch: {root}")
    if not tasks or set(tasks) != set(range(len(tasks))):
        raise ValueError(f"source task metadata is not contiguous: {root}")

    video_keys = _video_keys(info)
    episodes = []
    frame_count = 0
    for index in range(len(episode_rows)):
        row = episodes_by_index[index]
        length = _positive_int(row.get("length"), "episode length")
        row_tasks = row.get("tasks")
        if not isinstance(row_tasks, list) or not row_tasks:
            raise ValueError(f"source Episode {index} has no tasks")
        task_names = tuple(_string(task, "episode task") for task in row_tasks)
        if any(task not in tasks.values() for task in task_names):
            raise ValueError(f"source Episode {index} references an unknown task")
        source = provenance[index]
        _provenance_identity(source)
        data_path = root / format_v21_path(info, "data_path", index)
        _required_file(data_path)
        video_paths = {}
        for key in video_keys:
            path = root / format_v21_path(info, "video_path", index, key)
            _required_file(path)
            video_paths[key] = str(path)
        episodes.append(
            EpisodeDescriptor(
                index,
                length,
                task_names,
                source,
                {"data": str(data_path), "videos": video_paths, "episode": row},
            )
        )
        frame_count += length
    if info.get("total_frames") != frame_count:
        raise ValueError(f"source info total_frames mismatch: {root}")
    if info.get("total_tasks") != len(tasks):
        raise ValueError(f"source info total_tasks mismatch: {root}")
    if info.get("total_videos") != len(episodes) * len(video_keys):
        raise ValueError(f"source info total_videos mismatch: {root}")

    fingerprint = _fingerprint(required)
    return SnapshotDescriptor(
        name,
        root,
        repo_id,
        "lerobot-v2.1",
        fingerprint,
        info,
        snapshot,
        tuple(episodes),
        tasks,
        stats,
    )


def build_v21(
    plan: CompositionPlan,
    *,
    validator: Callable[..., dict[str, Any]] = validate_lerobot,
) -> CompositionResult:
    if plan.config.output.backend != "lerobot-v2.1":
        raise ValueError("v2.1 backend received another output format")
    if any(item.source.backend != "lerobot-v2.1" for item in plan.selected):
        raise ValueError("v2.1 output only accepts v2.1 sources")
    output = plan.config.output.path
    if output.exists():
        raise FileExistsError(output)
    baseline = plan.selected[0].source.info
    with preserved_staging_directory(output) as temporary:
        _write_journal(temporary, plan, 0, "building")
        build = _assemble(temporary, plan, baseline)
        _write_reports(temporary, plan, build)
        action_horizon = _action_horizon(plan.contract)
        validation = validator(
            temporary,
            plan.config.output.repo_id,
            action_horizon=action_horizon,
            expected_task=plan.tasks[0] if len(plan.tasks) == 1 else None,
        )
        validation["dataset_root"] = str(output)
        write_json(temporary / "reports" / "validation.json", validation)
        _write_journal(temporary, plan, len(plan.selected), "validated")
        _write_snapshot(temporary, plan, build)
    return CompositionResult(
        output,
        plan.config.output.repo_id,
        "lerobot-v2.1",
        len(plan.selected),
        plan.frame_count,
        plan.video_count,
        plan.tasks,
        plan.fingerprint,
    )


def format_v21_path(
    info: dict[str, Any],
    key: str,
    episode_index: int,
    video_key: str | None = None,
) -> Path:
    pattern = _string(info.get(key), key)
    chunks_size = _positive_int(info.get("chunks_size"), "chunks_size")
    values: dict[str, object] = {
        "episode_chunk": episode_index // chunks_size,
        "episode_index": episode_index,
    }
    if video_key is not None:
        values["video_key"] = video_key
    try:
        path = Path(pattern.format(**values))
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid LeRobot {key} template") from error
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"LeRobot {key} escapes dataset root")
    return path


def _assemble(target: Path, plan: CompositionPlan, baseline: dict[str, Any]) -> dict[str, Any]:
    chunks_size = _positive_int(baseline.get("chunks_size"), "chunks_size")
    global_tasks = {task: index for index, task in enumerate(plan.tasks)}
    episodes = []
    stats = []
    sources = []
    frame_offset = 0
    linked = copied = copied_bytes = 0
    video_keys = _video_keys(baseline)
    for global_episode, item in enumerate(plan.selected):
        episode = item.episode
        local_tasks = item.source.tasks
        source_data = Path(_string(episode.physical.get("data"), "source data"))
        target_data = target / format_v21_path(baseline, "data_path", global_episode)
        mapped_tasks = _rewrite_parquet(
            source_data,
            target_data,
            episode.episode_index,
            global_episode,
            frame_offset,
            local_tasks,
            global_tasks,
        )
        if len(mapped_tasks) != episode.length:
            raise ValueError("Parquet row count does not match Episode metadata")
        expected_tasks = {global_tasks[task] for task in episode.tasks}
        if set(mapped_tasks) != expected_tasks:
            raise ValueError("Parquet task indices do not match Episode metadata")
        source_videos = episode.physical.get("videos")
        if not isinstance(source_videos, dict):
            raise ValueError("source video mapping is invalid")
        for key in video_keys:
            source_video = Path(_string(source_videos.get(key), f"source video {key}"))
            target_video = target / format_v21_path(baseline, "video_path", global_episode, key)
            operation = _link_or_copy(source_video, target_video)
            if operation == "hardlink":
                linked += 1
            else:
                copied += 1
                copied_bytes += source_video.stat().st_size
        source_episode = episode.physical.get("episode")
        if not isinstance(source_episode, dict):
            raise ValueError("source Episode metadata is invalid")
        episodes.append({**source_episode, "episode_index": global_episode, "tasks": list(episode.tasks)})
        stats.append(
            _remap_stats(
                item.source.episode_stats[episode.episode_index],
                global_episode,
                frame_offset,
                mapped_tasks,
            )
        )
        sources.append(
            {
                **episode.provenance,
                "composition_source": item.source.name,
                "source_repo_id": item.source.repo_id,
                "source_lerobot_episode_index": episode.episode_index,
                "lerobot_episode_index": global_episode,
            }
        )
        frame_offset += episode.length
        _write_journal(target, plan, global_episode + 1, "building")
    meta = target / "meta"
    meta.mkdir(parents=True)
    info = deepcopy(baseline)
    info.update(
        total_episodes=len(episodes),
        total_frames=frame_offset,
        total_tasks=len(plan.tasks),
        total_videos=len(episodes) * len(video_keys),
        total_chunks=math.ceil(len(episodes) / chunks_size),
        splits={"train": f"0:{len(episodes)}"},
    )
    write_json(meta / "info.json", info)
    write_jsonl(meta / "episodes.jsonl", episodes)
    write_jsonl(meta / "episodes_stats.jsonl", stats)
    write_jsonl(meta / "tasks.jsonl", (
        {"task_index": index, "task": task} for index, task in enumerate(plan.tasks)
    ))
    return {
        "source_manifest": sources,
        "hardlink_count": linked,
        "copy_count": copied,
        "copied_bytes": copied_bytes,
    }


def _write_journal(
    target: Path,
    plan: CompositionPlan,
    completed_episodes: int,
    status: str,
) -> None:
    reports = target / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    write_json(
        reports / "composition-journal.json",
        {
            "schema_version": 1,
            "status": status,
            "plan_fingerprint": plan.fingerprint,
            "completed_episodes": completed_episodes,
            "episode_count": len(plan.selected),
        },
    )


def _rewrite_parquet(
    source: Path,
    target: Path,
    local_episode: int,
    global_episode: int,
    frame_offset: int,
    local_tasks: dict[int, str],
    global_tasks: dict[str, int],
) -> list[int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(source)
    required = {"episode_index", "frame_index", "index", "task_index"}
    missing = sorted(required - set(table.column_names))
    if missing:
        raise ValueError(f"Parquet is missing index columns: {missing}")
    rows = table.num_rows
    if set(table["episode_index"].to_pylist()) != {local_episode}:
        raise ValueError("Parquet episode_index does not match local Episode")
    if table["frame_index"].to_pylist() != list(range(rows)):
        raise ValueError("Parquet frame_index is not contiguous")
    mapped_tasks = []
    for value in table["task_index"].to_pylist():
        try:
            mapped_tasks.append(global_tasks[local_tasks[int(value)]])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Parquet references an unknown task_index: {value!r}") from error
    replacements = {
        "episode_index": [global_episode] * rows,
        "index": list(range(frame_offset, frame_offset + rows)),
        "task_index": mapped_tasks,
    }
    for name, values in replacements.items():
        index = table.schema.get_field_index(name)
        field = table.schema.field(index)
        table = table.set_column(index, field, pa.array(values, type=field.type))
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target)
    return mapped_tasks


def _remap_stats(
    row: dict[str, Any],
    global_episode: int,
    frame_offset: int,
    task_indices: list[int],
) -> dict[str, Any]:
    value = deepcopy(row)
    stats = value.get("stats")
    if not isinstance(stats, dict):
        raise ValueError("Episode stats must contain an object")
    value["episode_index"] = global_episode
    length = len(task_indices)
    stats["episode_index"] = _numeric_stats([global_episode] * length)
    stats["index"] = _numeric_stats(list(range(frame_offset, frame_offset + length)))
    stats["task_index"] = _numeric_stats(task_indices)
    return value


def _numeric_stats(values: list[int]) -> dict[str, list[int | float]]:
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "min": [min(values)],
        "max": [max(values)],
        "mean": [mean],
        "std": [math.sqrt(variance)],
        "count": [len(values)],
    }


def _write_reports(target: Path, plan: CompositionPlan, build: dict[str, Any]) -> None:
    reports = target / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    write_jsonl(reports / "source_manifest.jsonl", build["source_manifest"])
    write_jsonl(reports / "rejected.jsonl", ())
    write_json(
        reports / "composition.json",
        {
            "schema_version": 1,
            "backend": plan.config.output.backend,
            "repo_id": plan.config.output.repo_id,
            "plan_fingerprint": plan.fingerprint,
            "sources": [
                {
                    "name": source.name,
                    "path": str(source.root),
                    "repo_id": source.repo_id,
                    "backend": source.backend,
                    "metadata_fingerprint": source.metadata_fingerprint,
                    "selected_episodes": sum(item.source.name == source.name for item in plan.selected),
                }
                for source in _unique_sources(plan)
            ],
            "episode_count": len(plan.selected),
            "frame_count": plan.frame_count,
            "video_count": plan.video_count,
            "tasks": list(plan.tasks),
            "operations": {
                "hardlink": build["hardlink_count"],
                "copy": build["copy_count"],
                "copy_bytes": build["copied_bytes"],
                "packet_remux": 0,
            },
            "contract": plan.contract,
        },
    )


def _write_snapshot(target: Path, plan: CompositionPlan, build: dict[str, Any]) -> None:
    baseline = plan.selected[0].source.snapshot
    write_json(
        target / "snapshot.json",
        {
            "schema_version": 3,
            "status": "committed",
            "run_id": f"composition-{plan.fingerprint[:16]}",
            "repo_id": plan.config.output.repo_id,
            "builder_backend": plan.config.output.backend,
            "recipe": deepcopy(baseline.get("recipe", {})),
            "source_episode_count": len({
                item.episode.provenance["source_episode_id"] for item in plan.selected
            }),
            "fragment_count": len(_unique_sources(plan)),
            "episode_count": len(plan.selected),
            "frame_count": plan.frame_count,
            "video_count": plan.video_count,
            "tasks": list(plan.tasks),
            "source_manifest": "reports/source_manifest.jsonl",
            "composition_report": "reports/composition.json",
            "discarded_report": "reports/rejected.jsonl",
            "composition": {
                "schema_version": 1,
                "plan_fingerprint": plan.fingerprint,
                "operation_counts": {
                    "hardlink": build["hardlink_count"],
                    "copy": build["copy_count"],
                },
            },
        },
    )


def _unique_sources(plan: CompositionPlan) -> tuple[SnapshotDescriptor, ...]:
    sources = []
    seen = set()
    for item in plan.selected:
        if item.source.name not in seen:
            sources.append(item.source)
            seen.add(item.source.name)
    return tuple(sources)


def _link_or_copy(source: Path, target: Path) -> str:
    _required_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
        return "hardlink"
    except OSError as error:
        fallback = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EMLINK}
        if hasattr(errno, "ENOTSUP"):
            fallback.add(errno.ENOTSUP)
        if hasattr(errno, "EOPNOTSUPP"):
            fallback.add(errno.EOPNOTSUPP)
        if error.errno not in fallback:
            raise
    shutil.copy2(source, target)
    return "copy"


def _action_horizon(contract: dict[str, Any]) -> int:
    recipe = contract.get("recipe")
    if isinstance(recipe, dict):
        sampling = recipe.get("sampling_contract")
        if isinstance(sampling, dict):
            value = sampling.get("action_horizon")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return 50


def _fingerprint(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(paths[0].parent.parent)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _video_keys(info: dict[str, Any]) -> tuple[str, ...]:
    features = info.get("features")
    if not isinstance(features, dict):
        raise ValueError("LeRobot features must be an object")
    keys = tuple(sorted(
        key for key, value in features.items()
        if isinstance(value, dict) and value.get("dtype") == "video"
    ))
    if not keys:
        raise ValueError("LeRobot snapshot requires video features")
    return keys


def _indexed(rows: list[dict[str, Any]], key: str, label: str) -> dict[int, dict[str, Any]]:
    result = {}
    for row in rows:
        index = _non_negative_int(row.get(key), key)
        if index in result:
            raise ValueError(f"duplicate {label} index: {index}")
        result[index] = row
    return result


def _task_map(rows: list[dict[str, Any]]) -> dict[int, str]:
    result = {}
    for row in rows:
        index = _non_negative_int(row.get("task_index"), "task_index")
        if index in result:
            raise ValueError(f"duplicate task index: {index}")
        result[index] = _string(row.get("task"), "task")
    return result


def _provenance_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _string(row.get("segment_id"), "segment_id"),
        _string(row.get("source_episode_id"), "source_episode_id"),
        _string(row.get("source_session_id"), "source_session_id"),
    )


def _required_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"required source file is missing: {path}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    value = _non_negative_int(value, label)
    if value == 0:
        raise ValueError(f"{label} must be positive")
    return value
