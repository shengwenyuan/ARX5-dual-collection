from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any

from .models import CompositionConfig
from .models import OutputConfig
from .models import SourceConfig
from .models import V3RuntimeConfig


_BACKENDS = {"lerobot-v2.1", "lerobot-v3.0"}


def load_config(path: Path) -> CompositionConfig:
    path = path.resolve()
    payload = tomllib.loads(path.read_text())
    _keys(payload, {"schema_version", "output", "sources", "v3_runtime"}, "config")
    if payload.get("schema_version") != 1:
        raise ValueError("composition schema_version must be 1")

    output_row = _object(payload.get("output"), "output")
    _keys(output_row, {"backend", "repo_id", "path"}, "output")
    backend = _choice(output_row.get("backend"), _BACKENDS, "output.backend")
    repo_id = _repo_id(output_row.get("repo_id"))
    output_path = _absolute_path(output_row.get("path"), "output.path")
    if output_path.name != repo_id.rsplit("/", 1)[1]:
        raise ValueError("output.path basename must equal the repo_id dataset name")

    source_rows = payload.get("sources")
    if not isinstance(source_rows, list) or not source_rows:
        raise ValueError("sources must be a non-empty array")
    sources = []
    names: set[str] = set()
    roots: set[Path] = set()
    for index, raw in enumerate(source_rows):
        row = _object(raw, f"sources[{index}]")
        _keys(
            row,
            {"name", "path", "select_all", "selection_manifest"},
            f"sources[{index}]",
        )
        name = _string(row.get("name"), f"sources[{index}].name")
        root = _absolute_path(row.get("path"), f"sources[{index}].path")
        select_all = row.get("select_all", False)
        if not isinstance(select_all, bool):
            raise ValueError(f"sources[{index}].select_all must be a boolean")
        manifest_value = row.get("selection_manifest")
        manifest = None
        if manifest_value is not None:
            manifest = Path(
                _string(manifest_value, f"sources[{index}].selection_manifest")
            )
            if not manifest.is_absolute():
                manifest = (path.parent / manifest).resolve()
        if select_all == (manifest is not None):
            raise ValueError(
                f"sources[{index}] must use exactly one of select_all=true or selection_manifest"
            )
        if name in names:
            raise ValueError(f"duplicate source name: {name}")
        if root in roots:
            raise ValueError(f"duplicate source path: {root}")
        names.add(name)
        roots.add(root)
        sources.append(SourceConfig(name, root, select_all, manifest))

    runtime = None
    runtime_row = payload.get("v3_runtime")
    if runtime_row is not None:
        runtime_object = _object(runtime_row, "v3_runtime")
        _keys(runtime_object, {"python"}, "v3_runtime")
        runtime = V3RuntimeConfig(
            _absolute_path(runtime_object.get("python"), "v3_runtime.python")
        )
    if backend == "lerobot-v3.0" and runtime is None:
        raise ValueError("v3_runtime.python is required for lerobot-v3.0 output")
    if output_path in roots:
        raise ValueError("output.path must not equal a source path")
    return CompositionConfig(
        1,
        OutputConfig(backend, repo_id, output_path),
        tuple(sources),
        runtime,
        path,
    )


def _keys(payload: dict[str, Any], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise ValueError(f"unexpected {label} keys: {unexpected}")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _choice(value: object, choices: set[str], label: str) -> str:
    text = _string(value, label)
    if text not in choices:
        raise ValueError(f"{label} must be one of {sorted(choices)}")
    return text


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_string(value, label))
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path.resolve()


def _repo_id(value: object) -> str:
    repo_id = _string(value, "output.repo_id")
    owner, separator, dataset = repo_id.partition("/")
    if not separator or not owner or not dataset or "/" in dataset:
        raise ValueError("output.repo_id must use '<owner>/<dataset>'")
    return repo_id
