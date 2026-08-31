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
class PrefetchRuntimeConfig:
    pfs_root: Path
    streaming_root: Path
    stage_workers: int
    conversion_workers: int
    prefetch_target_bytes: int
    prefetch_max_bytes: int
    prefetch_max_episodes: int


@dataclass(frozen=True, slots=True)
class BufferedRuntimeConfig:
    pfs_root: Path
    streaming_root: Path
    stage_workers: int
    conversion_workers: int
    ready_low_bytes: int
    ready_high_bytes: int
    temporary_hard_max_bytes: int
    max_staged_episodes: int
    min_free_bytes: int


RuntimeSettings = RuntimeConfig | PrefetchRuntimeConfig | BufferedRuntimeConfig


@dataclass(frozen=True, slots=True)
class OutputConfig:
    lerobot_root: Path
    dataset_name: str
    repo_id: str

    def dated_path(self, today: date | None = None) -> Path:
        value = today or date.today()
        return (
            self.lerobot_root
            / self.repo_owner
            / f"{self.dataset_name}_{value.isoformat()}"
        )

    @property
    def repo_owner(self) -> str:
        return self.repo_id.partition("/")[0]

    def repo_id_for(self, output_path: Path) -> str:
        if not output_path.is_absolute():
            raise ValueError("LeRobot output path must be absolute")
        return f"{self.repo_owner}/{output_path.name}"


@dataclass(frozen=True, slots=True)
class RecipeConfig:
    name: str
    profile: str
    task: str | None = None
    task_source: str | None = None

    @property
    def task_identity(self) -> str:
        return self.task or self.task_source or ""

    def training_task(self, metadata_description: str) -> str:
        return metadata_description if self.task_source else self.task or ""


@dataclass(frozen=True, slots=True)
class StreamingConversionConfig:
    schema_version: int
    source: SourceConfig
    runtime: RuntimeSettings
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
        schema_version = payload["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {1, 2, 3}
        ):
            raise ValueError("streaming config schema_version must be 1, 2, or 3")

        source = _table(payload, "source")
        runtime = _table(payload, "runtime")
        output = _table(payload, "output")
        recipe = _table(payload, "recipe")
        _exact_keys(source, {"root", "include_paths", "block"}, "source")
        if schema_version == 1:
            _exact_keys(runtime, {"streaming_root", "workers"}, "runtime")
        elif schema_version == 2:
            _exact_keys(
                runtime,
                {
                    "pfs_root",
                    "streaming_root",
                    "stage_workers",
                    "conversion_workers",
                    "prefetch_target_bytes",
                    "prefetch_max_bytes",
                    "prefetch_max_episodes",
                },
                "runtime",
            )
        else:
            _exact_keys(
                runtime,
                {
                    "pfs_root",
                    "streaming_root",
                    "stage_workers",
                    "conversion_workers",
                    "ready_low_bytes",
                    "ready_high_bytes",
                    "temporary_hard_max_bytes",
                    "max_staged_episodes",
                    "min_free_bytes",
                },
                "runtime",
            )
        _exact_keys(
            output,
            {"lerobot_root", "dataset_name", "repo_id"},
            "output",
        )
        task_key = "task" if "task" in recipe else "task_source"
        _exact_keys(recipe, {"name", "profile", task_key}, "recipe")
        if ("task" in recipe) == ("task_source" in recipe):
            raise ValueError("recipe must use exactly one of task or task_source")

        runtime_config = _runtime_config(schema_version, runtime)
        dataset_name = _single_component(
            output["dataset_name"], "output.dataset_name"
        )
        repo_id = _repo_id(output["repo_id"])
        if repo_id.partition("/")[2] != dataset_name:
            raise ValueError("output.repo_id dataset must equal output.dataset_name")
        config = cls(
            schema_version=schema_version,
            source=SourceConfig(
                root=_absolute_path(source["root"], "source.root"),
                include_paths=_include_paths(source["include_paths"]),
                block=_block_names(source["block"]),
            ),
            runtime=runtime_config,
            output=OutputConfig(
                lerobot_root=_absolute_path(
                    output["lerobot_root"], "output.lerobot_root"
                ),
                dataset_name=dataset_name,
                repo_id=repo_id,
            ),
            recipe=RecipeConfig(
                name=_non_empty_string(recipe["name"], "recipe.name"),
                profile=_non_empty_string(recipe["profile"], "recipe.profile"),
                task=(
                    _non_empty_string(recipe["task"], "recipe.task")
                    if "task" in recipe
                    else None
                ),
                task_source=(
                    _task_source(recipe["task_source"])
                    if "task_source" in recipe
                    else None
                ),
            ),
        )
        if isinstance(runtime_config, (PrefetchRuntimeConfig, BufferedRuntimeConfig)):
            _require_within(
                config.output.lerobot_root,
                runtime_config.pfs_root,
                "output.lerobot_root",
            )
        return config


def _task_source(value: object) -> str:
    source = _non_empty_string(value, "recipe.task_source")
    if source != "metadata.task.description":
        raise ValueError("recipe.task_source must be 'metadata.task.description'")
    return source


def _runtime_config(
    schema_version: int,
    value: dict[str, object],
) -> RuntimeSettings:
    streaming_root = _absolute_path(value["streaming_root"], "runtime.streaming_root")
    if schema_version == 1:
        return RuntimeConfig(
            streaming_root=streaming_root,
            workers=_positive_int(value["workers"], "runtime.workers"),
        )

    pfs_root = _absolute_path(value["pfs_root"], "runtime.pfs_root")
    _require_within(streaming_root, pfs_root, "runtime.streaming_root")
    stage_workers = _positive_int(value["stage_workers"], "runtime.stage_workers")
    conversion_workers = _positive_int(
        value["conversion_workers"], "runtime.conversion_workers"
    )
    if schema_version == 2:
        target_bytes = _positive_int(
            value["prefetch_target_bytes"], "runtime.prefetch_target_bytes"
        )
        max_bytes = _positive_int(
            value["prefetch_max_bytes"], "runtime.prefetch_max_bytes"
        )
        if target_bytes > max_bytes:
            raise ValueError(
                "runtime.prefetch_target_bytes must not exceed prefetch_max_bytes"
            )
        return PrefetchRuntimeConfig(
            pfs_root=pfs_root,
            streaming_root=streaming_root,
            stage_workers=stage_workers,
            conversion_workers=conversion_workers,
            prefetch_target_bytes=target_bytes,
            prefetch_max_bytes=max_bytes,
            prefetch_max_episodes=_positive_int(
                value["prefetch_max_episodes"], "runtime.prefetch_max_episodes"
            ),
        )

    low_bytes = _positive_int(value["ready_low_bytes"], "runtime.ready_low_bytes")
    high_bytes = _positive_int(
        value["ready_high_bytes"], "runtime.ready_high_bytes"
    )
    hard_max_bytes = _positive_int(
        value["temporary_hard_max_bytes"], "runtime.temporary_hard_max_bytes"
    )
    min_free_bytes = _non_negative_int(
        value["min_free_bytes"], "runtime.min_free_bytes"
    )
    if low_bytes > high_bytes:
        raise ValueError("runtime.ready_low_bytes must not exceed ready_high_bytes")
    if high_bytes > hard_max_bytes:
        raise ValueError(
            "runtime.ready_high_bytes must not exceed temporary_hard_max_bytes"
        )
    return BufferedRuntimeConfig(
        pfs_root=pfs_root,
        streaming_root=streaming_root,
        stage_workers=stage_workers,
        conversion_workers=conversion_workers,
        ready_low_bytes=low_bytes,
        ready_high_bytes=high_bytes,
        temporary_hard_max_bytes=hard_max_bytes,
        max_staged_episodes=_positive_int(
            value["max_staged_episodes"], "runtime.max_staged_episodes"
        ),
        min_free_bytes=min_free_bytes,
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


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_within(path: Path, root: Path, label: str) -> None:
    normalized_path = path.resolve(strict=False)
    normalized_root = root.resolve(strict=False)
    if normalized_path == normalized_root or normalized_root not in normalized_path.parents:
        raise ValueError(f"{label} must be below runtime.pfs_root")


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


def _repo_id(value: object) -> str:
    text = _non_empty_string(value, "output.repo_id")
    owner, separator, dataset = text.partition("/")
    if not separator or not owner or not dataset or "/" in dataset:
        raise ValueError("output.repo_id must use the '<owner>/<dataset>' form")
    _single_component(owner, "output.repo_id owner")
    _single_component(dataset, "output.repo_id dataset")
    return text
