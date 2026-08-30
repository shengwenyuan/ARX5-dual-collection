from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from .v3 import V3_LEROBOT_VERSION


def run(request: dict[str, Any]) -> dict[str, Any]:
    installed = version("lerobot")
    if installed != V3_LEROBOT_VERSION:
        raise RuntimeError(
            f"v3 worker requires lerobot=={V3_LEROBOT_VERSION}, found {installed}"
        )
    operation = request.get("operation")
    if operation == "inspect":
        return _inspect(str(request["name"]), Path(request["root"]))
    if operation == "compose":
        return _compose(request)
    raise ValueError(f"unknown v3 worker operation: {operation!r}")


def _inspect(name: str, root: Path) -> dict[str, Any]:
    from lerobot.datasets.dataset_metadata import LeRobotDatasetMetadata

    snapshot = _read_object(root / "snapshot.json")
    info = _read_object(root / "meta" / "info.json")
    provenance = _read_jsonl(root / "reports" / "source_manifest.jsonl")
    if snapshot.get("status") != "committed" or snapshot.get("builder_backend") != "lerobot-v3.0":
        raise ValueError(f"source is not a committed project v3.0 snapshot: {root}")
    if info.get("codebase_version") != "v3.0":
        raise ValueError(f"source is not LeRobot v3.0: {root}")
    repo_id = str(snapshot["repo_id"])
    meta = LeRobotDatasetMetadata(repo_id, root=root)
    source_rows = {int(row["lerobot_episode_index"]): row for row in provenance}
    if set(source_rows) != set(range(meta.total_episodes)):
        raise ValueError("v3 source manifest does not cover every Episode")
    tasks = {
        int(meta.tasks.loc[task, "task_index"]): str(task)
        for task in meta.tasks.index.tolist()
    }
    episodes = []
    for index in range(meta.total_episodes):
        row = {key: _json_value(value) for key, value in meta.episodes[index].items()}
        episode_tasks = row.get("tasks")
        if not isinstance(episode_tasks, list) or not episode_tasks:
            raise ValueError(f"v3 Episode {index} has no tasks")
        video_shards = {
            key: [
                int(row[f"videos/{key}/chunk_index"]),
                int(row[f"videos/{key}/file_index"]),
            ]
            for key in meta.video_keys
        }
        episodes.append(
            {
                "episode_index": index,
                "length": int(row["length"]),
                "tasks": [str(task) for task in episode_tasks],
                "provenance": source_rows[index],
                "physical": {
                    "data_shard": [int(row["data/chunk_index"]), int(row["data/file_index"])],
                    "video_shards": video_shards,
                },
            }
        )
    return {
        "name": name,
        "root": str(root.resolve()),
        "repo_id": repo_id,
        "metadata_fingerprint": _metadata_fingerprint(root),
        "info": info,
        "snapshot": snapshot,
        "tasks": {str(key): value for key, value in tasks.items()},
        "episodes": episodes,
    }


def _compose(request: dict[str, Any]) -> dict[str, Any]:
    from lerobot.datasets import LeRobotDataset
    from lerobot.datasets.aggregate import aggregate_datasets
    from lerobot.datasets.dataset_metadata import LeRobotDatasetMetadata
    from lerobot.datasets.dataset_tools import split_dataset
    from lerobot.scripts.convert_dataset_v21_to_v30 import convert_dataset

    output = Path(request["output"])
    workspace = Path(request["workspace"])
    if output.exists():
        raise FileExistsError(output)
    workspace.mkdir(parents=True, exist_ok=False)
    roots = []
    repo_ids = []
    copied_v21 = 0
    split_v3 = 0
    for index, source in enumerate(request["sources"]):
        root = Path(source["root"])
        repo_id = str(source["repo_id"])
        indices = [int(value) for value in source["episode_indices"]]
        if source["backend"] == "lerobot-v2.1":
            convert_dataset(repo_id, root=root, push_to_hub=False, force_conversion=True)
            old = root.parent / f"{root.name}_old"
            if old.is_dir():
                shutil.rmtree(old)
            roots.append(root)
            repo_ids.append(repo_id)
            copied_v21 += 1
            continue
        metadata = LeRobotDatasetMetadata(repo_id, root=root)
        groups = _ordered_whole_shard_groups(metadata, indices)
        if groups == [list(range(metadata.total_episodes))]:
            roots.append(root)
            repo_ids.append(repo_id)
            continue
        dataset = LeRobotDataset(repo_id, root=root)
        for group_index, group in enumerate(groups):
            split_root = workspace / f"split-{index:03d}-{group_index:03d}"
            selected = split_dataset(dataset, {"selected": group}, output_dir=split_root)["selected"]
            roots.append(selected.root)
            repo_ids.append(selected.repo_id)
            split_v3 += 1

    aggregate_datasets(
        repo_ids=repo_ids,
        aggr_repo_id=str(request["repo_id"]),
        roots=roots,
        aggr_root=output,
        concatenate_videos=False,
        concatenate_data=False,
    )
    meta = LeRobotDatasetMetadata(str(request["repo_id"]), root=output)
    LeRobotDataset(str(request["repo_id"]), root=output)
    expected = request["expected"]
    tasks = [str(task) for task in meta.tasks.index.tolist()]
    observed = (meta.total_episodes, meta.total_frames, tasks)
    wanted = (int(expected["episodes"]), int(expected["frames"]), list(expected["tasks"]))
    if observed != wanted:
        raise ValueError(f"v3 aggregate totals disagree with plan: {observed} != {wanted}")
    video_files = len(list((output / "videos").glob("*/*/*.mp4"))) if (output / "videos").is_dir() else 0
    return {
        "codebase_version": "v3.0",
        "episodes": meta.total_episodes,
        "frames": meta.total_frames,
        "tasks": tasks,
        "video_files": video_files,
        "operations": {
            "v21_migrations": copied_v21,
            "v3_whole_shard_splits": split_v3,
            "video_copy": video_files,
            "video_reencode": 0,
            "video_packet_remux": 0,
        },
        "validation": {"metadata": "loaded", "dataset": "loaded"},
    }


def _ordered_whole_shard_groups(meta, indices: list[int]) -> list[list[int]]:
    selected = set(indices)
    if len(selected) != len(indices):
        raise ValueError("v3 source selection contains duplicate Episode indices")
    valid = set(range(meta.total_episodes))
    if not selected or not selected <= valid:
        raise ValueError("v3 source selection contains no Episodes or invalid indices")
    parent = {index: index for index in valid}

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for key in meta.video_keys:
        members: dict[tuple[int, int], set[int]] = {}
        for index in range(meta.total_episodes):
            row = meta.episodes[index]
            shard = (
                int(row[f"videos/{key}/chunk_index"]),
                int(row[f"videos/{key}/file_index"]),
            )
            members.setdefault(shard, set()).add(index)
        for shard, episodes in members.items():
            intersection = episodes & selected
            if intersection and intersection != episodes:
                raise ValueError(
                    f"selection cuts shared video shard {key}/{shard}; video re-encoding is forbidden"
                )
            values = sorted(episodes)
            for value in values[1:]:
                union(values[0], value)
    components: dict[int, list[int]] = {}
    for index in valid:
        components.setdefault(find(index), []).append(index)
    result = []
    cursor = 0
    while cursor < len(indices):
        component = sorted(components[find(indices[cursor])])
        if indices[cursor : cursor + len(component)] != component:
            raise ValueError("cannot reorder Episodes within a shared video-shard component")
        result.append(component)
        cursor += len(component)
    return result


def _metadata_fingerprint(root: Path) -> str:
    paths = [root / "snapshot.json", root / "reports" / "source_manifest.jsonl"]
    paths.extend(sorted((root / "meta").rglob("*")))
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _json_value(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, tuple):
        return list(value)
    return value


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result = []
    for line in path.read_text().splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object line: {path}")
            result.append(value)
    return result


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 2:
        raise SystemExit("usage: v3_worker REQUEST.json RESULT.json")
    request_path, result_path = map(Path, values)
    request = _read_object(request_path)
    result = run(request)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
