from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable, TextIO

from .alignment import build_alignment_report
from .alignment import render_alignment
from .alignment import require_enter_confirmation
from .builder import SnapshotBuildResult
from .builder import build_lerobot_v21_snapshot
from .config import SourceConfig
from .config import StreamingConversionConfig
from .coordinator import StreamingCoordinator
from .discovery import discover_episodes
from .manifest import RunManifest
from .models import DiscoveryResult
from .recipe import Pi05ConversionRecipe


@dataclass(frozen=True, slots=True)
class StreamingRunRequest:
    config_path: Path
    output_override: Path | None = None
    run_id: str | None = None
    resume_run_id: str | None = None
    retry_failed: bool = False


@dataclass(frozen=True, slots=True)
class StreamingApplicationResult:
    run_id: str
    snapshot: SnapshotBuildResult
    committed: int
    excluded: int
    discarded: int


Discovery = Callable[[SourceConfig], DiscoveryResult]
Builder = Callable[
    [RunManifest, Path, Pi05ConversionRecipe, str],
    SnapshotBuildResult,
]


def execute_streaming_conversion(
    request: StreamingRunRequest,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    discover: Discovery = discover_episodes,
    builder: Builder = build_lerobot_v21_snapshot,
    clock: Callable[[], datetime] | None = None,
) -> StreamingApplicationResult:
    _validate_request(request)
    config = StreamingConversionConfig.load(request.config_path)
    recipe = _load_recipe(request.config_path, config)

    if request.resume_run_id is not None:
        manifest = RunManifest.open(
            config.runtime.streaming_root / request.resume_run_id
        )
        _validate_resume_config(config, manifest)
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
    jobs = StreamingCoordinator(
        manifest,
        config.source.root,
        recipe,
        config.recipe.task,
        manifest.definition.repo_id,
        config.runtime.workers,
    ).run()
    snapshot = builder(
        manifest,
        manifest.definition.output_path,
        recipe,
        manifest.definition.repo_id,
    )
    states = [job.state.value for job in jobs.values()]
    return StreamingApplicationResult(
        run_id=manifest.run_id,
        snapshot=snapshot,
        committed=states.count("committed"),
        excluded=states.count("excluded"),
        discarded=states.count("discarded"),
    )


def _validate_request(request: StreamingRunRequest) -> None:
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


def _load_recipe(
    config_path: Path,
    config: StreamingConversionConfig,
) -> Pi05ConversionRecipe:
    profile = Path(config.recipe.profile)
    if not profile.is_absolute():
        profile = config_path.resolve().parent / profile
    recipe = Pi05ConversionRecipe.load(profile)
    if recipe.name != config.recipe.name:
        raise ValueError(
            f"streaming recipe name does not match profile: "
            f"{config.recipe.name!r} != {recipe.name!r}"
        )
    return recipe


def _validate_resume_config(
    config: StreamingConversionConfig,
    manifest: RunManifest,
) -> None:
    frozen = manifest.definition
    current = {
        "source_root": config.source.root.resolve(strict=True),
        "streaming_root": config.runtime.streaming_root,
        "repo_id": config.output.repo_id_for(frozen.output_path),
        "workers": config.runtime.workers,
        "recipe_name": config.recipe.name,
        "recipe_profile": config.recipe.profile,
        "recipe_task": config.recipe.task,
    }
    expected = {
        "source_root": frozen.source_root,
        "streaming_root": frozen.streaming_root,
        "repo_id": frozen.repo_id,
        "workers": frozen.workers,
        "recipe_name": frozen.recipe_name,
        "recipe_profile": frozen.recipe_profile,
        "recipe_task": frozen.recipe_task,
    }
    changed = [key for key, value in current.items() if value != expected[key]]
    if changed:
        raise ValueError(f"resume config differs from frozen run: {changed}")


def _default_run_id(value: datetime, dataset_name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("streaming application clock must be timezone-aware")
    timestamp = value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{dataset_name}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
