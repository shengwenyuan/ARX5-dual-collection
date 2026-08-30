from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from arx5_collection.artifacts import write_json
from arx5_collection.artifacts import write_jsonl

from .atomic import preserved_staging_directory
from .models import CompositionPlan
from .models import CompositionResult
from .models import EpisodeDescriptor
from .models import OutputConfig
from .models import SelectedEpisode
from .models import SnapshotDescriptor
from .v21 import build_v21


V3_LEROBOT_VERSION = "0.6.1"
V3_LEROBOT_COMMIT = "7e241bd630a3719a56157a497ce5d08f244784f1"


class V3WorkerClient:
    def __init__(self, python: Path) -> None:
        self.python = python

    def inspect(self, name: str, root: Path) -> SnapshotDescriptor:
        payload = self._call({"operation": "inspect", "name": name, "root": str(root)})
        return _descriptor_from_json(payload)

    def compose(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._call({"operation": "compose", **request})

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.python.is_file():
            raise ValueError(f"v3 worker Python is missing: {self.python}")
        with tempfile.TemporaryDirectory(prefix="arx5-lerobot-v3-protocol-") as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            result_path = root / "result.json"
            write_json(request_path, request)
            environment = os.environ.copy()
            environment["HF_HOME"] = str(root / "hf-home")
            environment["HF_DATASETS_CACHE"] = str(root / "hf-home" / "datasets")
            completed = subprocess.run(
                [
                    str(self.python),
                    "-m",
                    "arx5_collection.lerobot_recomposition.v3_worker",
                    str(request_path),
                    str(result_path),
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(f"LeRobot v3 worker failed: {detail}")
            if not result_path.is_file():
                raise RuntimeError("LeRobot v3 worker returned no result")
            result = json.loads(result_path.read_text())
            if not isinstance(result, dict):
                raise RuntimeError("LeRobot v3 worker result is not an object")
            return result


def build_v3(plan: CompositionPlan) -> CompositionResult:
    if plan.config.output.backend != "lerobot-v3.0" or plan.config.v3_runtime is None:
        raise ValueError("v3 backend requires output.backend=lerobot-v3.0 and v3_runtime")
    output = plan.config.output.path
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    client = V3WorkerClient(plan.config.v3_runtime.python)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.v3-inputs.", dir=output.parent) as workspace_value:
        workspace = Path(workspace_value)
        sources = _materialize_worker_sources(plan, workspace)
        with preserved_staging_directory(output) as temporary:
            _write_v3_journal(temporary, plan, "building")
            dataset_target = temporary / "dataset"
            result = client.compose(
                {
                    "repo_id": plan.config.output.repo_id,
                    "output": str(dataset_target),
                    "workspace": str(workspace / "worker"),
                    "sources": sources,
                    "expected": {
                        "episodes": len(plan.selected),
                        "frames": plan.frame_count,
                        "tasks": list(plan.tasks),
                    },
                }
            )
            _validate_worker_result(plan, result)
            _promote_worker_dataset(dataset_target, temporary)
            _write_v3_sidecars(temporary, plan, result)
            _write_v3_journal(temporary, plan, "validated")
    return CompositionResult(
        output,
        plan.config.output.repo_id,
        "lerobot-v3.0",
        len(plan.selected),
        plan.frame_count,
        int(result.get("video_files", 0)),
        plan.tasks,
        plan.fingerprint,
    )


def _materialize_worker_sources(plan: CompositionPlan, workspace: Path) -> list[dict[str, Any]]:
    grouped: list[tuple[SnapshotDescriptor, list[SelectedEpisode]]] = []
    positions: dict[str, int] = {}
    for item in plan.selected:
        position = positions.get(item.source.name)
        if position is None:
            positions[item.source.name] = len(grouped)
            grouped.append((item.source, [item]))
        else:
            grouped[position][1].append(item)
    result = []
    for source, selected in grouped:
        if source.backend == "lerobot-v2.1":
            dataset_name = f"input-{len(result):03d}-{source.name}"
            target = workspace / dataset_name
            output = OutputConfig("lerobot-v2.1", f"local/{dataset_name}", target)
            source_plan = CompositionPlan(
                replace(plan.config, output=output),
                tuple(selected),
                _ordered_tasks(selected),
                sum(item.episode.length for item in selected),
                len(selected) * len(_video_keys(source.info)),
                plan.contract,
                plan.fingerprint,
            )
            build_v21(source_plan, validator=lambda *_args, **_kwargs: {"status": "staged-for-v3"})
            result.append(
                {
                    "name": source.name,
                    "backend": source.backend,
                    "root": str(target),
                    "repo_id": output.repo_id,
                    "episode_indices": list(range(len(selected))),
                }
            )
        else:
            result.append(
                {
                    "name": source.name,
                    "backend": source.backend,
                    "root": str(source.root),
                    "repo_id": source.repo_id,
                    "episode_indices": [item.episode.episode_index for item in selected],
                }
            )
    return result


def _write_v3_sidecars(target: Path, plan: CompositionPlan, worker: dict[str, Any]) -> None:
    reports = target / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    rows = []
    for output_index, item in enumerate(plan.selected):
        rows.append(
            {
                **item.episode.provenance,
                "composition_source": item.source.name,
                "source_repo_id": item.source.repo_id,
                "source_lerobot_episode_index": item.episode.episode_index,
                "lerobot_episode_index": output_index,
            }
        )
    write_jsonl(reports / "source_manifest.jsonl", rows)
    write_jsonl(reports / "rejected.jsonl", ())
    write_json(
        reports / "validation.json",
        {
            "status": "ready",
            "dataset_root": str(plan.config.output.path),
            "backend": "lerobot-v3.0",
            "worker": worker.get("validation", {}),
        },
    )
    composition = {
        "schema_version": 1,
        "backend": "lerobot-v3.0",
        "repo_id": plan.config.output.repo_id,
        "plan_fingerprint": plan.fingerprint,
        "lerobot_version": V3_LEROBOT_VERSION,
        "lerobot_commit": V3_LEROBOT_COMMIT,
        "episode_count": len(plan.selected),
        "frame_count": plan.frame_count,
        "video_count": worker.get("video_files", 0),
        "tasks": list(plan.tasks),
        "operations": worker.get("operations", {}),
        "contract": plan.contract,
    }
    write_json(reports / "composition.json", composition)
    baseline_recipe = plan.selected[0].source.snapshot.get("recipe", {})
    write_json(
        target / "snapshot.json",
        {
            "schema_version": 3,
            "status": "committed",
            "run_id": f"composition-{plan.fingerprint[:16]}",
            "repo_id": plan.config.output.repo_id,
            "builder_backend": "lerobot-v3.0",
            "recipe": baseline_recipe,
            "source_episode_count": len({
                item.episode.provenance["source_episode_id"] for item in plan.selected
            }),
            "fragment_count": len({item.source.name for item in plan.selected}),
            "episode_count": len(plan.selected),
            "frame_count": plan.frame_count,
            "video_count": worker.get("video_files", 0),
            "tasks": list(plan.tasks),
            "source_manifest": "reports/source_manifest.jsonl",
            "composition_report": "reports/composition.json",
            "discarded_report": "reports/rejected.jsonl",
            "composition": {
                "schema_version": 1,
                "plan_fingerprint": plan.fingerprint,
                "lerobot_commit": V3_LEROBOT_COMMIT,
            },
        },
    )


def _write_v3_journal(target: Path, plan: CompositionPlan, status: str) -> None:
    reports = target / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    write_json(
        reports / "composition-journal.json",
        {
            "schema_version": 1,
            "status": status,
            "plan_fingerprint": plan.fingerprint,
            "episode_count": len(plan.selected),
        },
    )


def _promote_worker_dataset(dataset: Path, target: Path) -> None:
    if not dataset.is_dir():
        raise ValueError("v3 worker did not create its dataset directory")
    for child in dataset.iterdir():
        destination = target / child.name
        if destination.exists():
            raise FileExistsError(destination)
        os.replace(child, destination)
    dataset.rmdir()


def _validate_worker_result(plan: CompositionPlan, result: dict[str, Any]) -> None:
    expected = (len(plan.selected), plan.frame_count, list(plan.tasks))
    observed = (result.get("episodes"), result.get("frames"), result.get("tasks"))
    if observed != expected:
        raise ValueError(f"v3 worker result does not match frozen plan: {observed} != {expected}")
    if result.get("codebase_version") != "v3.0":
        raise ValueError("v3 worker did not produce codebase_version v3.0")


def _descriptor_from_json(payload: dict[str, Any]) -> SnapshotDescriptor:
    episodes = tuple(
        EpisodeDescriptor(
            int(row["episode_index"]),
            int(row["length"]),
            tuple(row["tasks"]),
            dict(row["provenance"]),
            dict(row["physical"]),
        )
        for row in payload["episodes"]
    )
    return SnapshotDescriptor(
        str(payload["name"]),
        Path(payload["root"]),
        str(payload["repo_id"]),
        "lerobot-v3.0",
        str(payload["metadata_fingerprint"]),
        dict(payload["info"]),
        dict(payload["snapshot"]),
        episodes,
        {int(key): str(value) for key, value in payload["tasks"].items()},
        {},
    )


def _ordered_tasks(selected: list[SelectedEpisode]) -> tuple[str, ...]:
    result = []
    for item in selected:
        for task in item.episode.tasks:
            if task not in result:
                result.append(task)
    return tuple(result)


def _video_keys(info: dict[str, Any]) -> tuple[str, ...]:
    features = info.get("features", {})
    return tuple(
        key for key, value in features.items()
        if isinstance(value, dict) and value.get("dtype") == "video"
    )
