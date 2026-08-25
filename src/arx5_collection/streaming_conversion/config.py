from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import tomllib


@dataclass(frozen=True, slots=True)
class SourceConfig:
    root: Path
    include_paths: tuple[Path, ...]
    block: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    streaming_root: Path
    workers: int


@dataclass(frozen=True, slots=True)
class OutputConfig:
    lerobot_root: Path
    dataset_name: str
    repo_id: str

    def dated_path(self, today: date | None = None) -> Path:
        value = today or date.today()
        return self.lerobot_root / f"{self.dataset_name}_{value.isoformat()}"


@dataclass(frozen=True, slots=True)
class RecipeConfig:
    name: str
    profile: str
    task: str


@dataclass(frozen=True, slots=True)
class StreamingConversionConfig:
    schema_version: int
    source: SourceConfig
    runtime: RuntimeConfig
    output: OutputConfig
    recipe: RecipeConfig

    @classmethod
    def load(cls, path: str | Path) -> StreamingConversionConfig:
        with Path(path).open("rb") as stream:
            payload = tomllib.load(stream)
        _exact_keys(
            payload,
            {"schema_version", "source", "runtime", "output", "recipe"},
            "config",
        )
        if payload["schema_version"] != 1:
            raise ValueError("streaming config schema_version must be 1")

        source = _table(payload, "source")
        runtime = _table(payload, "runtime")
        output = _table(payload, "output")
        recipe = _table(payload, "recipe")
        _exact_keys(source, {"root", "include_paths", "block"}, "source")
        _exact_keys(runtime, {"streaming_root", "workers"}, "runtime")
        _exact_keys(
            output,
            {"lerobot_root", "dataset_name", "repo_id"},
            "output",
        )
        _exact_keys(recipe, {"name", "profile", "task"}, "recipe")

        workers = runtime["workers"]
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("runtime.workers must be a positive integer")
        return cls(
            schema_version=1,
            source=SourceConfig(
                root=_absolute_path(source["root"], "source.root"),
                include_paths=_include_paths(source["include_paths"]),
                block=_block_names(source["block"]),
            ),
            runtime=RuntimeConfig(
                streaming_root=_absolute_path(
                    runtime["streaming_root"], "runtime.streaming_root"
                ),
                workers=workers,
            ),
            output=OutputConfig(
                lerobot_root=_absolute_path(
                    output["lerobot_root"], "output.lerobot_root"
                ),
                dataset_name=_single_component(
                    output["dataset_name"], "output.dataset_name"
                ),
                repo_id=_non_empty_string(output["repo_id"], "output.repo_id"),
            ),
            recipe=RecipeConfig(
                name=_non_empty_string(recipe["name"], "recipe.name"),
                profile=_non_empty_string(recipe["profile"], "recipe.profile"),
                task=_non_empty_string(recipe["task"], "recipe.task"),
            ),
        )


def _table(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"streaming config must contain a [{name}] table")
    return value


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} keys must be exactly {sorted(expected)}")


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_non_empty_string(value, label))
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


def _single_component(value: object, label: str) -> str:
    text = _non_empty_string(value, label)
    if text in {".", ".."} or Path(text).name != text:
        raise ValueError(f"{label} must be one path component")
    return text


def _include_paths(value: object) -> tuple[Path, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("source.include_paths must be a non-empty array")
    paths = []
    for item in value:
        text = _non_empty_string(item, "source.include_paths item")
        path = Path(text)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("source.include_paths entries must be normalized relative paths")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ValueError("source.include_paths entries must be unique")
    return tuple(paths)


def _block_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("source.block must be an array")
    names = tuple(_single_component(item, "source.block item") for item in value)
    if len(names) != len(set(names)):
        raise ValueError("source.block entries must be unique")
    return names
