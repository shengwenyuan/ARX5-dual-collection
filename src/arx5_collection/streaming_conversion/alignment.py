from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

from .config import BufferedRuntimeConfig
from .config import PrefetchRuntimeConfig
from .config import StreamingConversionConfig
from .models import DiscoveryResult


PROMPT = "确认以上目录与 Episode 集合，按 ENTER 开始；输入其他内容或 Ctrl+C 取消："


class AlignmentCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AlignmentReport:
    config: StreamingConversionConfig
    discovery: DiscoveryResult
    output_path: Path


def build_alignment_report(
    config: StreamingConversionConfig,
    discovery: DiscoveryResult,
    output_override: Path | None = None,
    *,
    today: date | None = None,
) -> AlignmentReport:
    output_path = output_override or config.output.dated_path(today)
    if not output_path.is_absolute():
        raise ValueError("streaming output override must be an absolute path")
    if (
        isinstance(config.runtime, (PrefetchRuntimeConfig, BufferedRuntimeConfig))
        and config.runtime.pfs_root.resolve(strict=False)
        not in output_path.resolve(strict=False).parents
    ):
        raise ValueError("streaming output must be below runtime.pfs_root")
    return AlignmentReport(config, discovery, output_path)


def render_alignment(report: AlignmentReport) -> str:
    discovery = report.discovery
    lines = [
        "ARX5 streaming conversion alignment",
        f"source_root: {discovery.source_root}",
        f"streaming_root: {report.config.runtime.streaming_root}",
        f"output: {report.output_path}",
    ]
    runtime = report.config.runtime
    if isinstance(runtime, BufferedRuntimeConfig):
        lines.extend(
            [
                "runtime_mode: buffered_prefetch",
                f"pfs_root: {runtime.pfs_root}",
                f"stage_workers: {runtime.stage_workers}",
                f"conversion_workers: {runtime.conversion_workers}",
                f"ready_low_bytes: {runtime.ready_low_bytes}",
                f"ready_high_bytes: {runtime.ready_high_bytes}",
                f"temporary_hard_max_bytes: {runtime.temporary_hard_max_bytes}",
                f"max_staged_episodes: {runtime.max_staged_episodes}",
                f"min_free_bytes: {runtime.min_free_bytes}",
            ]
        )
    elif isinstance(runtime, PrefetchRuntimeConfig):
        lines.extend(
            [
                "runtime_mode: bounded_prefetch",
                f"pfs_root: {runtime.pfs_root}",
                f"stage_workers: {runtime.stage_workers}",
                f"conversion_workers: {runtime.conversion_workers}",
                f"prefetch_target_bytes: {runtime.prefetch_target_bytes}",
                f"prefetch_max_bytes: {runtime.prefetch_max_bytes}",
                f"prefetch_max_episodes: {runtime.prefetch_max_episodes}",
            ]
        )
    else:
        lines.extend(
            [
                "runtime_mode: legacy_shared_pool",
                f"workers: {runtime.workers}",
            ]
        )
    if report.config.recipe.task_source:
        lines.append(f"training_task_source: {report.config.recipe.task_source}")
        lines.append(f"training_tasks: {discovery.task_counts()}")
    else:
        lines.append(f"training_task: {report.config.recipe.task}")
    lines.append("include_paths:")
    lines.extend(f"  - {path}" for path in report.config.source.include_paths)
    lines.append("block:")
    lines.extend(f"  - {name}" for name in report.config.source.block)
    if not report.config.source.block:
        lines.append("  - <none>")
    lines.extend(
        [
            f"episodes: {len(discovery.candidates)}",
            f"mcap_bytes: {discovery.total_mcap_bytes}",
            f"collection_types: {discovery.collection_type_counts()}",
            f"outcomes: {discovery.outcome_counts()}",
            f"metadata_tasks: {discovery.task_counts()}",
            f"blocked_dirs: {len(discovery.blocked_dirs)}",
            "candidates:",
        ]
    )
    lines.extend(
        f"  - {item.relative_dir} | {item.collection_type} | "
        f"{item.outcome} | {item.mcap.size} bytes"
        for item in discovery.candidates
    )
    if not discovery.candidates:
        lines.append("  - <none>")
    return "\n".join(lines) + "\n"


def require_enter_confirmation(input_stream: TextIO, output_stream: TextIO) -> None:
    if not input_stream.isatty():
        raise AlignmentCancelled("alignment confirmation requires an interactive TTY")
    output_stream.write(PROMPT)
    output_stream.flush()
    response = input_stream.readline()
    if response in {"\n", "\r\n"}:
        return
    if response == "":
        raise AlignmentCancelled("alignment confirmation cancelled by EOF")
    raise AlignmentCancelled("alignment confirmation cancelled by non-empty input")
