from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

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
    return AlignmentReport(config, discovery, output_path)


def render_alignment(report: AlignmentReport) -> str:
    discovery = report.discovery
    lines = [
        "ARX5 streaming conversion alignment",
        f"source_root: {discovery.source_root}",
        f"streaming_root: {report.config.runtime.streaming_root}",
        f"output: {report.output_path}",
        f"workers: {report.config.runtime.workers}",
        f"training_task: {report.config.recipe.task}",
        "include_paths:",
    ]
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
