from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path
import shutil
from typing import Any

from arx5_collection.artifacts import read_json
from arx5_collection.artifacts import read_jsonl
from arx5_collection.artifacts import write_json
from arx5_collection.artifacts import write_jsonl
from arx5_collection.atomic import staged_directory
from arx5_collection.pi05_dataset.validate import validate_lerobot

from .manifest import RunManifest
from .models import JobState
from .models import SelectionEntry
from .recipe import Pi05ConversionRecipe
from .worker import FRAGMENT_SCHEMA_VERSION
from .worker import load_committed_fragment


SNAPSHOT_SCHEMA_VERSION = 2
_ACTIVE_STATES = {
    JobState.DISCOVERED,
    JobState.STAGING,
    JobState.CONVERTING,
    JobState.VALIDATING,
}
_COMPATIBLE_CONTRACT_KEYS = (
    "openpi_commit",
    "lerobot_commit",
    "fps",
    "mode",
    "image_size",
    "image_color",
    "state_action_order",
    "state_action_version",
    "filter_version",
    "gripper_calibration",
    "sampling_contract",
)


@dataclass(frozen=True, slots=True)
class FragmentDescriptor:
    episode_id: str
    root: Path
    fragment: dict[str, Any]
    dataset: Path
    info: dict[str, Any]
    episodes: tuple[dict[str, Any], ...]
    episode_stats: dict[int, dict[str, Any]]
    tasks: dict[int, str]
    sources: dict[int, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SnapshotBuildResult:
    output_path: Path
    repo_id: str
    source_episode_count: int
    fragment_count: int
    episode_count: int
    frame_count: int
    tasks: tuple[str, ...]


def build_lerobot_v21_snapshot(
    manifest: RunManifest,
    output_path: Path,
    recipe: Pi05ConversionRecipe,
    repo_id: str,
) -> SnapshotBuildResult:
    """Build one immutable LeRobot v2.1 snapshot from this run's Fragments."""

    _validate_target(output_path, repo_id, recipe)
    committed, omitted = _partition_selection(manifest)
    descriptors = tuple(
        _load_fragment(manifest.run_dir, selection, recipe)
        for selection in committed
    )
    _validate_fragment_compatibility(descriptors)
    tasks = _ordered_tasks(descriptors)
    _validate_task_scope(descriptors)

    with staged_directory(output_path) as temporary:
        build = _assemble_v21(temporary, repo_id, descriptors, tasks)
        _write_reports(
            temporary,
            manifest,
            recipe,
            repo_id,
            descriptors,
            omitted,
            build,
            tasks,
        )
        validation = validate_lerobot(
            temporary,
            repo_id,
            action_horizon=recipe.selection.action_horizon,
            expected_task=tasks[0] if len(tasks) == 1 else None,
        )
        validation["dataset_root"] = str(output_path.resolve())
        write_json(temporary / "reports" / "validation.json", validation)

    _remove_run_cache(manifest.run_dir)
    return SnapshotBuildResult(
        output_path=output_path,
        repo_id=repo_id,
        source_episode_count=len(committed),
        fragment_count=len(descriptors),
        episode_count=build["episode_count"],
        frame_count=build["frame_count"],
        tasks=tasks,
    )


def _validate_target(
    output_path: Path,
    repo_id: str,
    recipe: Pi05ConversionRecipe,
) -> None:
    if not output_path.is_absolute():
        raise ValueError("snapshot output path must be absolute")
    if output_path.exists():
        raise FileExistsError(output_path)
    owner, separator, name = repo_id.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repo_id must use the '<owner>/<dataset>' form")
    if recipe.builder_backend != "lerobot-v2.1":
        raise ValueError("snapshot Builder requires the lerobot-v2.1 backend")


def _partition_selection(
    manifest: RunManifest,
) -> tuple[tuple[SelectionEntry, ...], tuple[SelectionEntry, ...]]:
    jobs = manifest.jobs
    active = [
        item.episode_id
        for item in manifest.selection
        if jobs[item.episode_id].state in _ACTIVE_STATES
    ]
    failed = [
        item.episode_id
        for item in manifest.selection
        if jobs[item.episode_id].state is JobState.FAILED
    ]
    if active:
        raise RuntimeError(f"snapshot run has non-terminal Episodes: {active}")
    if failed:
        raise RuntimeError(f"snapshot run has failed Episodes: {failed}")
    committed = tuple(
        item
        for item in manifest.selection
        if jobs[item.episode_id].state is JobState.COMMITTED
    )
    if not committed:
        raise ValueError("snapshot run has no committed Fragments")
    omitted = tuple(item for item in manifest.selection if item not in committed)
    return committed, omitted


def _load_fragment(
    run_dir: Path,
    selection: SelectionEntry,
    recipe: Pi05ConversionRecipe,
) -> FragmentDescriptor:
    root = run_dir / "fragments" / selection.episode_id
    result = load_committed_fragment(root)
    fragment = read_json(root / "fragment.json")
    if result.episode_id != selection.episode_id:
        raise ValueError("Fragment does not match frozen selection")
    _validate_fragment_recipe(fragment, recipe)
    dataset = root / _relative_path(fragment, "dataset")
    info = read_json(dataset / "meta" / "info.json")
    episodes = tuple(
        sorted(
            read_jsonl(dataset / "meta" / "episodes.jsonl"),
            key=lambda row: _non_negative_int(row.get("episode_index"), "episode_index"),
        )
    )
    stats_rows = read_jsonl(dataset / "meta" / "episodes_stats.jsonl")
    task_rows = read_jsonl(dataset / "meta" / "tasks.jsonl")
    source_rows = read_jsonl(root / _relative_path(fragment, "source_manifest"))
    episode_stats = _indexed_rows(stats_rows, "episode_index", "episode stats")
    tasks = _task_map(task_rows)
    sources = _indexed_rows(source_rows, "lerobot_episode_index", "source manifest")
    _validate_fragment_dataset(
        selection,
        fragment,
        dataset,
        info,
        episodes,
        episode_stats,
        tasks,
        sources,
    )
    return FragmentDescriptor(
        selection.episode_id,
        root,
        fragment,
        dataset,
        info,
        episodes,
        episode_stats,
        tasks,
        sources,
    )


def _validate_fragment_recipe(
    fragment: dict[str, Any],
    recipe: Pi05ConversionRecipe,
) -> None:
    if fragment.get("schema_version") != FRAGMENT_SCHEMA_VERSION:
        raise ValueError("unsupported Fragment schema_version")
    value = fragment.get("recipe")
    if not isinstance(value, dict):
        raise ValueError("Fragment recipe must be an object")
    expected = {
        "schema_version": recipe.schema_version,
        "name": recipe.name,
        "builder_backend": recipe.builder_backend,
        "gripper_normalization": recipe.gripper_normalization,
        "gripper_contract": recipe.gripper_contract,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"Fragment recipe mismatch: {key}")
    expected_gripper = {
        "contract_id": recipe.gripper_contract,
        "left": asdict(recipe.gripper),
        "right": asdict(recipe.gripper),
    }
    contracts = fragment.get("contracts")
    if (
        not isinstance(contracts, dict)
        or contracts.get("gripper_calibration") != expected_gripper
    ):
        raise ValueError("Fragment gripper calibration does not match device contract")


def _validate_fragment_dataset(
    selection: SelectionEntry,
    fragment: dict[str, Any],
    dataset: Path,
    info: dict[str, Any],
    episodes: tuple[dict[str, Any], ...],
    episode_stats: dict[int, dict[str, Any]],
    tasks: dict[int, str],
    sources: dict[int, dict[str, Any]],
) -> None:
    episode_count = _positive_int(fragment.get("segment_count"), "segment_count")
    frame_count = _positive_int(fragment.get("frame_count"), "frame_count")
    expected_indices = set(range(episode_count))
    observed_indices = {
        _non_negative_int(row.get("episode_index"), "episode_index")
        for row in episodes
    }
    if observed_indices != expected_indices:
        raise ValueError("Fragment LeRobot episode indices are not contiguous")
    if set(episode_stats) != expected_indices or set(sources) != expected_indices:
        raise ValueError("Fragment metadata does not cover every local episode")
    if info.get("codebase_version") != "v2.1":
        raise ValueError("Fragment is not LeRobot v2.1")
    _validate_info_contract(fragment, info)
    if info.get("total_episodes") != episode_count or info.get("total_frames") != frame_count:
        raise ValueError("Fragment LeRobot totals do not match fragment.json")
    if info.get("total_tasks") != len(tasks) or not tasks:
        raise ValueError("Fragment task metadata is inconsistent")
    if set(tasks) != set(range(len(tasks))):
        raise ValueError("Fragment task indices are not contiguous")
    if sum(_positive_int(row.get("length"), "episode length") for row in episodes) != frame_count:
        raise ValueError("Fragment episode lengths do not match frame count")
    training_task = _string(fragment.get("training_task"), "training_task")
    observed_sessions: set[str] = set()
    for local_index, row in enumerate(episodes):
        row_tasks = row.get("tasks")
        if not isinstance(row_tasks, list) or not row_tasks:
            raise ValueError("Fragment episode has no tasks")
        if any(task not in tasks.values() for task in row_tasks):
            raise ValueError("Fragment episode references an unknown task")
        if set(row_tasks) != {training_task}:
            raise ValueError("Fragment Episode task does not match training_task")
        source = sources[local_index]
        if source.get("source_episode_id") != selection.episode_id:
            raise ValueError("Fragment source manifest references another Episode")
        if source.get("split_group") != selection.episode_id:
            raise ValueError("Fragment split_group must equal source Episode")
        observed_sessions.add(
            _string(source.get("source_session_id"), "source_session_id")
        )
        _required_file(dataset / _format_path(info, "data_path", local_index))
        for video_key in _video_keys(info):
            _required_file(
                dataset / _format_path(info, "video_path", local_index, video_key)
            )
    if fragment.get("source_session_ids") != sorted(observed_sessions):
        raise ValueError("Fragment source Session summary is inconsistent")
    expected_videos = episode_count * len(_video_keys(info))
    if info.get("total_videos") != expected_videos:
        raise ValueError("Fragment video count is inconsistent")


def _validate_info_contract(
    fragment: dict[str, Any],
    info: dict[str, Any],
) -> None:
    contracts = fragment["contracts"]
    if info.get("fps") != contracts.get("fps"):
        raise ValueError("Fragment fps disagrees with LeRobot info")
    image_size = contracts.get("image_size")
    if (
        not isinstance(image_size, list)
        or len(image_size) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in image_size)
    ):
        raise ValueError("Fragment image_size contract is invalid")
    width, height = image_size
    features = info.get("features")
    if not isinstance(features, dict):
        raise ValueError("LeRobot features must be an object")
    order = contracts.get("state_action_order")
    for key in ("observation.state", "action"):
        feature = features.get(key)
        if not isinstance(feature, dict) or feature.get("names") != [order]:
            raise ValueError(f"LeRobot feature disagrees with state/action order: {key}")
    for key in _video_keys(info):
        feature = features[key]
        if feature.get("shape") != [3, height, width]:
            raise ValueError(f"LeRobot video feature has an unexpected shape: {key}")
    if contracts.get("image_color") != "RGB":
        raise ValueError("Fragment image color must be RGB")


def _validate_fragment_compatibility(
    descriptors: tuple[FragmentDescriptor, ...],
) -> None:
    baseline = descriptors[0]
    baseline_contract = _contract_signature(baseline.fragment)
    baseline_info = _info_signature(baseline.info)
    seen_segments: set[str] = set()
    for descriptor in descriptors:
        if _contract_signature(descriptor.fragment) != baseline_contract:
            raise ValueError(f"Fragment training contract drift: {descriptor.episode_id}")
        if _info_signature(descriptor.info) != baseline_info:
            raise ValueError(f"Fragment LeRobot schema drift: {descriptor.episode_id}")
        for source in descriptor.sources.values():
            segment_id = _string(source.get("segment_id"), "segment_id")
            if segment_id in seen_segments:
                raise ValueError(f"duplicate segment across Fragments: {segment_id}")
            seen_segments.add(segment_id)


def _contract_signature(fragment: dict[str, Any]) -> dict[str, Any]:
    contracts = fragment.get("contracts")
    if not isinstance(contracts, dict):
        raise ValueError("Fragment contracts must be an object")
    missing = [key for key in _COMPATIBLE_CONTRACT_KEYS if key not in contracts]
    if missing:
        raise ValueError(f"Fragment contracts are incomplete: {missing}")
    if contracts["mode"] != "video":
        raise ValueError("lerobot-v2.1 Builder currently requires video Fragments")
    return {key: contracts[key] for key in _COMPATIBLE_CONTRACT_KEYS}


def _info_signature(info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "codebase_version",
        "robot_type",
        "chunks_size",
        "fps",
        "data_path",
        "video_path",
        "features",
    )
    missing = [key for key in keys if key not in info]
    if missing:
        raise ValueError(f"LeRobot info is incomplete: {missing}")
    return {key: info[key] for key in keys}


def _ordered_tasks(descriptors: tuple[FragmentDescriptor, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    for descriptor in descriptors:
        for episode in descriptor.episodes:
            for task in episode["tasks"]:
                if task not in ordered:
                    ordered.append(task)
    if not ordered:
        raise ValueError("snapshot contains no task")
    return tuple(ordered)


def _validate_task_scope(descriptors: tuple[FragmentDescriptor, ...]) -> None:
    episode_tasks: dict[str, set[str]] = {}
    session_tasks: dict[str, set[str]] = {}
    for descriptor in descriptors:
        for local_index, episode in enumerate(descriptor.episodes):
            tasks = {_string(value, "episode task") for value in episode["tasks"]}
            source = descriptor.sources[local_index]
            episode_id = _string(source.get("source_episode_id"), "source_episode_id")
            session_id = _string(source.get("source_session_id"), "source_session_id")
            episode_tasks.setdefault(episode_id, set()).update(tasks)
            session_tasks.setdefault(session_id, set()).update(tasks)
    for episode_id, tasks in episode_tasks.items():
        if len(tasks) != 1:
            raise ValueError(f"task mismatch within source Episode: {episode_id}")
    for session_id, tasks in session_tasks.items():
        if len(tasks) != 1:
            raise ValueError(f"task mismatch within source Session: {session_id}")


def _assemble_v21(
    target: Path,
    repo_id: str,
    descriptors: tuple[FragmentDescriptor, ...],
    tasks: tuple[str, ...],
) -> dict[str, Any]:
    baseline = descriptors[0].info
    chunks_size = _positive_int(baseline.get("chunks_size"), "chunks_size")
    task_indices = {task: index for index, task in enumerate(tasks)}
    output_episodes: list[dict[str, Any]] = []
    output_stats: list[dict[str, Any]] = []
    output_sources: list[dict[str, Any]] = []
    frame_offset = 0
    global_episode = 0
    video_count = 0

    for descriptor in descriptors:
        local_tasks = descriptor.tasks
        for episode in descriptor.episodes:
            local_episode = _non_negative_int(
                episode.get("episode_index"), "episode_index"
            )
            length = _positive_int(episode.get("length"), "episode length")
            source_data = descriptor.dataset / _format_path(
                descriptor.info, "data_path", local_episode
            )
            target_data = target / _format_path(
                baseline, "data_path", global_episode
            )
            mapped_task_indices = _rewrite_episode_parquet(
                source_data,
                target_data,
                local_episode,
                global_episode,
                frame_offset,
                local_tasks,
                task_indices,
            )
            if len(mapped_task_indices) != length:
                raise ValueError("Parquet row count does not match Episode metadata")
            episode_tasks = [_string(value, "episode task") for value in episode["tasks"]]
            if set(mapped_task_indices) != {task_indices[task] for task in episode_tasks}:
                raise ValueError("Parquet task indices do not match Episode metadata")

            for video_key in _video_keys(baseline):
                source_video = descriptor.dataset / _format_path(
                    descriptor.info,
                    "video_path",
                    local_episode,
                    video_key,
                )
                target_video = target / _format_path(
                    baseline,
                    "video_path",
                    global_episode,
                    video_key,
                )
                _link_or_copy(source_video, target_video)
                video_count += 1

            output_episodes.append(
                {
                    **episode,
                    "episode_index": global_episode,
                    "tasks": episode_tasks,
                }
            )
            output_stats.append(
                _remap_stats(
                    descriptor.episode_stats[local_episode],
                    global_episode,
                    frame_offset,
                    mapped_task_indices,
                )
            )
            output_sources.append(
                {
                    **descriptor.sources[local_episode],
                    "lerobot_episode_index": global_episode,
                }
            )
            frame_offset += length
            global_episode += 1

    meta = target / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    info = deepcopy(baseline)
    info.update(
        {
            "total_episodes": global_episode,
            "total_frames": frame_offset,
            "total_tasks": len(tasks),
            "total_videos": video_count,
            "total_chunks": math.ceil(global_episode / chunks_size),
            "splits": {"train": f"0:{global_episode}"},
        }
    )
    write_json(meta / "info.json", info)
    write_jsonl(meta / "episodes.jsonl", output_episodes)
    write_jsonl(meta / "episodes_stats.jsonl", output_stats)
    write_jsonl(
        meta / "tasks.jsonl",
        [{"task_index": index, "task": task} for index, task in enumerate(tasks)],
    )
    return {
        "repo_id": repo_id,
        "episode_count": global_episode,
        "frame_count": frame_offset,
        "video_count": video_count,
        "source_manifest": output_sources,
    }


def _rewrite_episode_parquet(
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
    if not required.issubset(table.column_names):
        missing = sorted(required - set(table.column_names))
        raise ValueError(f"Parquet is missing index columns: {missing}")
    rows = table.num_rows
    observed_episode = table["episode_index"].to_pylist()
    if set(observed_episode) != {local_episode}:
        raise ValueError("Parquet episode_index does not match local Episode")
    frame_indices = table["frame_index"].to_pylist()
    if frame_indices != list(range(rows)):
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
        column = table.schema.get_field_index(name)
        field = table.schema.field(column)
        table = table.set_column(column, field, pa.array(values, type=field.type))
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
    _non_negative_int(value.get("episode_index"), "stats episode_index")
    stats = value.get("stats")
    if not isinstance(stats, dict):
        raise ValueError("Episode stats must contain an object")
    length = len(task_indices)
    value["episode_index"] = global_episode
    stats["episode_index"] = _numeric_stats([global_episode] * length)
    stats["index"] = _numeric_stats(list(range(frame_offset, frame_offset + length)))
    stats["task_index"] = _numeric_stats(task_indices)
    return value


def _numeric_stats(values: list[int]) -> dict[str, list[int | float]]:
    if not values:
        raise ValueError("cannot build stats for an empty Episode")
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "min": [min(values)],
        "max": [max(values)],
        "mean": [mean],
        "std": [math.sqrt(variance)],
        "count": [len(values)],
    }


def _write_reports(
    target: Path,
    manifest: RunManifest,
    recipe: Pi05ConversionRecipe,
    repo_id: str,
    descriptors: tuple[FragmentDescriptor, ...],
    omitted: tuple[SelectionEntry, ...],
    build: dict[str, Any],
    tasks: tuple[str, ...],
) -> None:
    reports = target / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    write_jsonl(reports / "source_manifest.jsonl", build["source_manifest"])
    write_jsonl(
        reports / "discarded.jsonl",
        [
            {
                "episode_id": selection.episode_id,
                "source_dir": str(selection.source_dir),
                "state": manifest.jobs[selection.episode_id].state.value,
                "reason_code": manifest.jobs[selection.episode_id].reason_code,
                "detail": manifest.jobs[selection.episode_id].detail,
            }
            for selection in omitted
        ],
    )
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "status": "committed",
        "run_id": manifest.run_id,
        "repo_id": repo_id,
        "builder_backend": recipe.builder_backend,
        "recipe": {
            "schema_version": recipe.schema_version,
            "name": recipe.name,
            "gripper_normalization": recipe.gripper_normalization,
            "gripper_contract": recipe.gripper_contract,
        },
        "source_episode_count": len(descriptors),
        "fragment_count": len(descriptors),
        "episode_count": build["episode_count"],
        "frame_count": build["frame_count"],
        "video_count": build["video_count"],
        "tasks": list(tasks),
        "source_manifest": "reports/source_manifest.jsonl",
        "discarded_report": "reports/discarded.jsonl",
    }
    write_json(target / "snapshot.json", snapshot)


def _format_path(
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


def _video_keys(info: dict[str, Any]) -> tuple[str, ...]:
    features = info.get("features")
    if not isinstance(features, dict):
        raise ValueError("LeRobot features must be an object")
    keys = tuple(
        sorted(
            key
            for key, value in features.items()
            if isinstance(value, dict) and value.get("dtype") == "video"
        )
    )
    if not keys:
        raise ValueError("LeRobot snapshot requires video features")
    return keys


def _indexed_rows(
    rows: list[dict[str, Any]],
    key: str,
    label: str,
) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for row in rows:
        index = _non_negative_int(row.get(key), key)
        if index in indexed:
            raise ValueError(f"duplicate {label} index: {index}")
        indexed[index] = row
    return indexed


def _task_map(rows: list[dict[str, Any]]) -> dict[int, str]:
    tasks: dict[int, str] = {}
    for row in rows:
        index = _non_negative_int(row.get("task_index"), "task_index")
        if index in tasks:
            raise ValueError(f"duplicate task index: {index}")
        tasks[index] = _string(row.get("task"), "task")
    return tasks


def _relative_path(fragment: dict[str, Any], name: str) -> Path:
    paths = fragment.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("Fragment paths must be an object")
    path = Path(_string(paths.get(name), f"Fragment path {name}"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Fragment path escapes root: {name}")
    return path


def _required_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Fragment file is missing: {path}")


def _link_or_copy(source: Path, target: Path) -> None:
    _required_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP}:
            raise
        shutil.copy2(source, target)


def _remove_run_cache(run_dir: Path) -> None:
    for name in ("staging", "fragments"):
        path = run_dir / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"run cache path is not a directory: {path}")
        shutil.rmtree(path)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    number = _non_negative_int(value, label)
    if number == 0:
        raise ValueError(f"{label} must be positive")
    return number
