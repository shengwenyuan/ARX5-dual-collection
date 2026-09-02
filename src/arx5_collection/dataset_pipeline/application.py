from __future__ import annotations

from dataclasses import dataclass
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Callable, TextIO

from .configuration.recipe import BUILTIN_RECIPE_PREFIX
from .configuration.recipe import DatasetPipelineRecipe
from .configuration.run import BufferedRuntimeConfig
from .configuration.run import DatasetPipelineConfig
from .configuration.run import PrefetchRuntimeConfig
from .configuration.run import SourceConfig
from .execution.confirmation import build_alignment_report
from .execution.confirmation import render_alignment
from .execution.confirmation import require_enter_confirmation
from .execution.coordinator import CoordinatorMetric
from .execution.coordinator import CoordinatorProgress
from .execution.coordinator import StreamingCoordinator
from .execution.models import DiscoveryResult
from .execution.models import JobSnapshot
from .execution.models import JobState
from .mining_stage.dataset_generator.lerobot_dataset_merge import SnapshotBuildResult
from .mining_stage.dataset_generator.lerobot_dataset_merge import (
    build_lerobot_v21_snapshot,
)
from .persistence.manifest import RunManifest
from .source.discovery import discover_episodes


@dataclass(frozen=True, slots=True)
class DatasetPipelineRequest:
    config_path: Path
    output_override: Path | None = None
    run_id: str | None = None
    resume_run_id: str | None = None
    retry_failed: bool = False


@dataclass(frozen=True, slots=True)
class DatasetPipelineResult:
    run_id: str
    snapshot: SnapshotBuildResult
    committed: int
    excluded: int
    discarded: int


Discovery = Callable[[SourceConfig], DiscoveryResult]
Builder = Callable[
    [RunManifest, Path, DatasetPipelineRecipe, str],
    SnapshotBuildResult,
]


def execute_dataset_pipeline(
    request: DatasetPipelineRequest,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    discover: Discovery = discover_episodes,
    builder: Builder = build_lerobot_v21_snapshot,
    clock: Callable[[], datetime] | None = None,
) -> DatasetPipelineResult:
    config = DatasetPipelineConfig.load(request.config_path)
    return execute_dataset_pipeline_config(
        config,
        request,
        input_stream,
        output_stream,
        discover=discover,
        builder=builder,
        clock=clock,
    )


def execute_dataset_pipeline_config(
    config: DatasetPipelineConfig,
    request: DatasetPipelineRequest,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    discover: Discovery = discover_episodes,
    builder: Builder = build_lerobot_v21_snapshot,
    clock: Callable[[], datetime] | None = None,
) -> DatasetPipelineResult:
    _validate_request(request)
    recipe = load_pipeline_recipe(request.config_path, config)

    if request.resume_run_id is not None:
        manifest = RunManifest.open(
            config.runtime.streaming_root / request.resume_run_id
        )
        if request.retry_failed:
            for episode_id in manifest.retryable_episode_ids():
                manifest.retry_failed(episode_id, detail="operator requested CLI retry")
    else:
        now = (clock or _utc_now)()
        discovery = discover(config.source)
        report = build_alignment_report(
            config,
            discovery,
            request.output_override,
            today=now.date(),
        )
        if report.output_path.exists():
            raise FileExistsError(report.output_path)
        run_id = request.run_id or _default_run_id(now, config.output.dataset_name)
        run_dir = config.runtime.streaming_root / run_id
        if run_dir.exists():
            raise FileExistsError(run_dir)
        output_stream.write(render_alignment(report))
        output_stream.flush()
        require_enter_confirmation(input_stream, output_stream)
        manifest = RunManifest.create(
            config,
            discovery,
            report.output_path,
            run_id,
            repo_id=config.output.repo_id_for(report.output_path),
        )

    return execute_frozen_streaming_run(
        config,
        recipe,
        manifest,
        output_stream,
        builder=builder,
    )


def execute_frozen_streaming_run(
    config: DatasetPipelineConfig,
    recipe: DatasetPipelineRecipe,
    manifest: RunManifest,
    output_stream: TextIO,
    *,
    builder: Builder = build_lerobot_v21_snapshot,
) -> DatasetPipelineResult:
    """Execute an already frozen run without repeating operator alignment."""

    _validate_resume_config(config, manifest)
    if recipe.name != config.recipe.name:
        raise ValueError(
            "dataset pipeline recipe name does not match profile: "
            f"{config.recipe.name!r} != {recipe.name!r}"
        )
    output_stream.write(
        json.dumps(
            {
                "run_id": manifest.run_id,
                "run_dir": str(manifest.run_dir),
                "output": str(manifest.definition.output_path),
            },
            sort_keys=True,
        )
        + "\n"
    )
    output_stream.flush()
    metrics = _MetricsJournal(manifest.run_dir / "metrics.jsonl")
    jobs = StreamingCoordinator(
        manifest,
        config.source.root,
        recipe,
        manifest.definition.repo_id,
        config.runtime,
        source_materialization=config.source.materialization,
        progress_reporter=lambda progress: _write_progress(
            output_stream, progress, metrics
        ),
        metric_reporter=metrics.write_work,
    ).run()
    snapshot = builder(
        manifest,
        manifest.definition.output_path,
        recipe,
        manifest.definition.repo_id,
    )
    removed_source = _remove_committed_direct_source(config, jobs)
    if removed_source is not None:
        output_stream.write(
            json.dumps(
                {
                    "direct_source_cleanup": {
                        "path": str(removed_source),
                        "status": "deleted",
                    }
                },
                sort_keys=True,
            )
            + "\n"
        )
        output_stream.flush()
    states = [job.state.value for job in jobs.values()]
    return DatasetPipelineResult(
        run_id=manifest.run_id,
        snapshot=snapshot,
        committed=states.count("committed"),
        excluded=states.count("excluded"),
        discarded=states.count("discarded"),
    )


def _remove_committed_direct_source(
    config: DatasetPipelineConfig,
    jobs: dict[str, JobSnapshot],
) -> Path | None:
    if config.source.materialization != "direct":
        return None
    states = {job.state for job in jobs.values()}
    if JobState.COMMITTED not in states or states & {
        JobState.DISCOVERED,
        JobState.STAGING,
        JobState.CONVERTING,
        JobState.VALIDATING,
        JobState.FAILED,
    }:
        return None
    runtime = config.runtime
    if not isinstance(runtime, (PrefetchRuntimeConfig, BufferedRuntimeConfig)):
        raise RuntimeError("direct source cleanup requires a PFS runtime")
    source = config.source.root
    if source.is_symlink():
        raise RuntimeError("refusing to delete a symbolic-link direct source")
    resolved = source.resolve(strict=True)
    temporary_root = (runtime.pfs_root / "tmp").resolve(strict=True)
    if not resolved.is_dir() or resolved.parent != temporary_root:
        raise RuntimeError(
            "refusing to delete direct source outside pfs_root/tmp/<dataset>"
        )
    shutil.rmtree(resolved)
    return resolved


def _validate_request(request: DatasetPipelineRequest) -> None:
    if request.run_id is not None and request.resume_run_id is not None:
        raise ValueError("--run-id and --resume are mutually exclusive")
    if request.resume_run_id is not None and request.output_override is not None:
        raise ValueError("--output cannot change a resumed run")
    if request.retry_failed and request.resume_run_id is None:
        raise ValueError("--retry-failed requires --resume")
    for label, value in (
        ("--run-id", request.run_id),
        ("--resume", request.resume_run_id),
    ):
        if value is not None and (
            value in {".", ".."} or not value or Path(value).name != value
        ):
            raise ValueError(f"{label} must be one path component")


def load_pipeline_recipe(
    config_path: Path,
    config: DatasetPipelineConfig,
) -> DatasetPipelineRecipe:
    profile_value = config.recipe.profile
    if profile_value.startswith(BUILTIN_RECIPE_PREFIX):
        profile: str | Path = profile_value
    else:
        profile = Path(profile_value)
        if not profile.is_absolute():
            profile = config_path.resolve().parent / profile
    recipe = DatasetPipelineRecipe.load(profile)
    if recipe.name != config.recipe.name:
        raise ValueError(
            f"dataset pipeline recipe name does not match profile: "
            f"{config.recipe.name!r} != {recipe.name!r}"
        )
    return recipe


def _validate_resume_config(
    config: DatasetPipelineConfig,
    manifest: RunManifest,
) -> None:
    frozen = manifest.definition
    current = {
        "source_root": config.source.root.resolve(strict=True),
        "source_materialization": config.source.materialization,
        "streaming_root": config.runtime.streaming_root,
        "repo_id": config.output.repo_id_for(frozen.output_path),
        "recipe_name": config.recipe.name,
        "recipe_profile": config.recipe.profile,
        "recipe_task": config.recipe.task_identity,
    }
    expected = {
        "source_root": frozen.source_root,
        "source_materialization": frozen.source_materialization,
        "streaming_root": frozen.streaming_root,
        "repo_id": frozen.repo_id,
        "recipe_name": frozen.recipe_name,
        "recipe_profile": frozen.recipe_profile,
        "recipe_task": frozen.recipe_task,
    }
    current.update(_config_runtime_values(config))
    expected.update(_frozen_runtime_values(manifest))
    changed = [key for key, value in current.items() if value != expected[key]]
    if changed:
        raise ValueError(f"resume config differs from frozen run: {changed}")


def _config_runtime_values(config: DatasetPipelineConfig) -> dict[str, object]:
    runtime = config.runtime
    if isinstance(runtime, BufferedRuntimeConfig):
        return {
            "runtime_mode": "buffered_prefetch",
            "pfs_root": runtime.pfs_root,
            "stage_workers": runtime.stage_workers,
            "conversion_workers": runtime.conversion_workers,
            "ready_low_bytes": runtime.ready_low_bytes,
            "ready_high_bytes": runtime.ready_high_bytes,
            "temporary_hard_max_bytes": runtime.temporary_hard_max_bytes,
            "max_staged_episodes": runtime.max_staged_episodes,
            "min_free_bytes": runtime.min_free_bytes,
        }
    if isinstance(runtime, PrefetchRuntimeConfig):
        return {
            "runtime_mode": "bounded_prefetch",
            "pfs_root": runtime.pfs_root,
            "stage_workers": runtime.stage_workers,
            "conversion_workers": runtime.conversion_workers,
            "prefetch_target_bytes": runtime.prefetch_target_bytes,
            "prefetch_max_bytes": runtime.prefetch_max_bytes,
            "prefetch_max_episodes": runtime.prefetch_max_episodes,
        }
    return {
        "runtime_mode": "legacy_shared_pool",
        "workers": runtime.workers,
    }


def _frozen_runtime_values(manifest: RunManifest) -> dict[str, object]:
    frozen = manifest.definition
    if frozen.workers is not None:
        return {
            "runtime_mode": "legacy_shared_pool",
            "workers": frozen.workers,
        }
    if frozen.ready_low_bytes is not None:
        return {
            "runtime_mode": "buffered_prefetch",
            "pfs_root": frozen.pfs_root,
            "stage_workers": frozen.stage_workers,
            "conversion_workers": frozen.conversion_workers,
            "ready_low_bytes": frozen.ready_low_bytes,
            "ready_high_bytes": frozen.ready_high_bytes,
            "temporary_hard_max_bytes": frozen.temporary_hard_max_bytes,
            "max_staged_episodes": frozen.max_staged_episodes,
            "min_free_bytes": frozen.min_free_bytes,
        }
    return {
        "runtime_mode": "bounded_prefetch",
        "pfs_root": frozen.pfs_root,
        "stage_workers": frozen.stage_workers,
        "conversion_workers": frozen.conversion_workers,
        "prefetch_target_bytes": frozen.prefetch_target_bytes,
        "prefetch_max_bytes": frozen.prefetch_max_bytes,
        "prefetch_max_episodes": frozen.prefetch_max_episodes,
    }


def _write_progress(
    output_stream: TextIO,
    progress: CoordinatorProgress,
    metrics: _MetricsJournal,
) -> None:
    value = asdict(progress)
    output_stream.write(
        json.dumps({"streaming_progress": value}, sort_keys=True) + "\n"
    )
    output_stream.flush()
    metrics.write("progress", value)


class _MetricsJournal:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write_work(self, metric: CoordinatorMetric) -> None:
        value = asdict(metric)
        value["phase_seconds"] = dict(metric.phase_seconds)
        self.write("work_completed", value)

    def write(self, kind: str, value: dict[str, object]) -> None:
        record = {
            "schema_version": 1,
            "recorded_at": _utc_now().isoformat(),
            "kind": kind,
            **value,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _default_run_id(value: datetime, dataset_name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("streaming application clock must be timezone-aware")
    timestamp = value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{dataset_name}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
