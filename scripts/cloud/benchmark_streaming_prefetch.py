#!/usr/bin/env python3
"""Benchmark frozen streaming samples without changing production runs."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
import traceback
from typing import Any

from arx5_collection.streaming_conversion.application import (
    execute_frozen_streaming_run,
    load_conversion_recipe,
)
from arx5_collection.streaming_conversion.config import (
    BufferedRuntimeConfig,
    PrefetchRuntimeConfig,
    StreamingConversionConfig,
)
from arx5_collection.streaming_conversion.coordinator import StageWork, stage_episode
from arx5_collection.streaming_conversion.manifest import RunManifest
from arx5_collection.streaming_conversion.models import (
    DiscoveryResult,
    EpisodeCandidate,
    FileIdentity,
    SelectionEntry,
)


_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class FrozenEntry:
    row: dict[str, Any]
    selection: SelectionEntry

    @property
    def source_bytes(self) -> int:
        return self.selection.mcap.size + self.selection.metadata.size


def _read_manifest(path: Path) -> list[FrozenEntry]:
    entries = []
    with path.open() as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            try:
                selection = SelectionEntry(
                    episode_id=row["episode_id"],
                    source_session_id=row["source_session_id"],
                    source_dir=Path(row["source_dir"]),
                    relative_dir=Path(row["relative_dir"]),
                    collection_type=row["collection_type"],
                    outcome=row["outcome"],
                    metadata_task_id=row["metadata_task"]["id"],
                    metadata_task_description=row["metadata_task"]["description"],
                    training_task=row["training_task"],
                    mcap=FileIdentity(**row["mcap"]),
                    metadata=FileIdentity(**row["metadata"]),
                )
            except (KeyError, TypeError) as error:
                raise ValueError(f"invalid manifest row {line_number}: {error}") from error
            entries.append(FrozenEntry(row, selection))
    if not entries:
        raise ValueError("benchmark manifest is empty")
    episode_ids = [entry.selection.episode_id for entry in entries]
    if len(set(episode_ids)) != len(episode_ids):
        raise ValueError("benchmark manifest contains duplicate episode_id")
    return entries


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    root = root.resolve(strict=True)
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"{label} must be below {root}: {path}")
    return resolved


def _run_id(label: str) -> str:
    if not _LABEL.fullmatch(label):
        raise ValueError("label must contain only lowercase letters, digits, and hyphens")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{label}"


def _sample_groups(
    entries: list[FrozenEntry],
) -> dict[tuple[str, str, int], list[FrozenEntry]]:
    ordered = sorted(
        entries,
        key=lambda entry: (entry.source_bytes, entry.selection.episode_id),
    )
    size_band = {
        entry.selection.episode_id: min(2, index * 3 // len(ordered))
        for index, entry in enumerate(ordered)
    }
    groups: dict[tuple[str, str, int], list[FrozenEntry]] = defaultdict(list)
    for entry in entries:
        parts = entry.selection.source_session_id.split("/")
        station = parts[0] if parts else "unknown"
        day = parts[1] if len(parts) > 1 else "unknown"
        groups[(station, day, size_band[entry.selection.episode_id])].append(entry)
    for values in groups.values():
        values.sort(key=lambda entry: (entry.source_bytes, entry.selection.episode_id))
    return groups


def _stratified(entries: list[FrozenEntry], count: int) -> list[FrozenEntry]:
    if count < 1 or count > len(entries):
        raise ValueError("sample count must be within the manifest size")
    groups = _sample_groups(entries)
    selected = []
    while len(selected) < count:
        changed = False
        for key in sorted(groups):
            if groups[key] and len(selected) < count:
                selected.append(groups[key].pop(len(groups[key]) // 2))
                changed = True
        if not changed:
            break
    return sorted(selected, key=lambda entry: entry.selection.episode_id)


def run_sample(args: argparse.Namespace) -> int:
    entries = _read_manifest(args.manifest)
    if args.episode_id:
        by_id = {entry.selection.episode_id: entry for entry in entries}
        missing = sorted(set(args.episode_id) - set(by_id))
        if missing:
            raise ValueError(f"whitelist episode_id not in manifest: {missing}")
        selected = [by_id[episode_id] for episode_id in args.episode_id]
        strategy = "explicit_whitelist"
    else:
        selected = _stratified(entries, args.count)
        strategy = "station_day_size_stratified"
    if args.output.exists():
        raise FileExistsError(args.output)
    _write_jsonl(args.output, [entry.row for entry in selected])
    _write_json(
        args.output.with_suffix(args.output.suffix + ".summary.json"),
        {
            "schema_version": 1,
            "strategy": strategy,
            "source_manifest": str(args.manifest.resolve()),
            "episodes": [entry.selection.episode_id for entry in selected],
            "source_bytes": sum(entry.source_bytes for entry in selected),
        },
    )
    return 0


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def _resources(cgroup_root: Path) -> dict[str, int]:
    result = {}
    current = _read_int(cgroup_root / "memory.current")
    if current is not None:
        result["memory_current"] = current
    try:
        lines = (cgroup_root / "memory.stat").read_text().splitlines()
    except (FileNotFoundError, PermissionError):
        lines = []
    memory_stat = {key: int(value) for key, value in (line.split() for line in lines)}
    for key in ("anon", "file", "kernel"):
        if key in memory_stat:
            result[f"memory_{key}"] = memory_stat[key]
    try:
        lines = (cgroup_root / "cpu.stat").read_text().splitlines()
    except (FileNotFoundError, PermissionError):
        lines = []
    for key, value in (line.split() for line in lines):
        result[f"cpu_{key}"] = int(value)
    try:
        lines = (cgroup_root / "io.pressure").read_text().splitlines()
    except (FileNotFoundError, PermissionError):
        lines = []
    for line in lines:
        kind, *fields = line.split()
        for field in fields:
            key, value = field.split("=", 1)
            if key == "total":
                result[f"io_pressure_{kind}_total"] = int(value)
    return result


class ResourceSampler:
    def __init__(self, cgroup_root: Path) -> None:
        self.cgroup_root = cgroup_root
        self.samples: list[dict[str, int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> ResourceSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(2.0):
            self.samples.append(_resources(self.cgroup_root))

    def summary(self) -> dict[str, int]:
        keys = {key for sample in self.samples for key in sample}
        return {
            f"peak_{key}": max(sample.get(key, 0) for sample in self.samples)
            for key in keys
        }


def _cpu_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = set(before) | set(after)
    return {
        key: after.get(key, 0) - before.get(key, 0)
        for key in sorted(keys)
        if key.startswith("cpu_")
    }


def _remove_isolated(path: Path, root: Path) -> None:
    path = _inside(path, root, "cleanup path")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _workers(value: str) -> list[int]:
    result = [int(item) for item in value.split(",")]
    if not result or any(item < 1 for item in result):
        raise argparse.ArgumentTypeError("workers must be comma-separated positive ints")
    return result


def run_stage(args: argparse.Namespace) -> int:
    entries = _read_manifest(args.manifest)
    containment = args.containment_root.resolve(strict=True)
    benchmark_root = _inside(args.benchmark_root, containment, "benchmark root")
    benchmark_root.mkdir(parents=True, exist_ok=True)
    source_root = args.source_root.resolve(strict=True)
    source_bytes = sum(entry.source_bytes for entry in entries)
    campaign = _run_id(args.label)
    for workers in args.workers:
        target = benchmark_root / "data" / campaign / f"stage-w{workers}"
        target.mkdir(parents=True)
        before = _resources(args.cgroup_root)
        started = time.monotonic()
        errors = []
        with ResourceSampler(args.cgroup_root) as sampler:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        stage_episode,
                        StageWork(
                            source_root,
                            entry.selection,
                            target / entry.selection.episode_id,
                        ),
                    ): entry.selection.episode_id
                    for entry in entries
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:
                        errors.append(
                            {
                                "episode_id": futures[future],
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
        elapsed = time.monotonic() - started
        after = _resources(args.cgroup_root)
        metric = {
            "schema_version": 2,
            "phase": "bos_stage",
            "campaign": campaign,
            "workers": workers,
            "episodes": len(entries),
            "source_bytes": source_bytes,
            "elapsed_seconds": elapsed,
            "source_gb_s": source_bytes / 1_000_000_000 / elapsed,
            "errors": errors,
            "resource": sampler.summary(),
            "cpu_delta": _cpu_delta(before, after),
        }
        metric_path = (
            benchmark_root / "results" / f"{campaign}-stage-w{workers}.json"
        )
        _write_json(metric_path, metric)
        print(json.dumps(metric, sort_keys=True), flush=True)
        _remove_isolated(target, benchmark_root)
        if errors:
            return 2
    return 0


def _discovery(source_root: Path, entries: list[FrozenEntry]) -> DiscoveryResult:
    candidates = []
    for entry in entries:
        value = entry.selection
        candidates.append(
            EpisodeCandidate(
                source_dir=value.source_dir,
                relative_dir=value.relative_dir,
                include_path=Path(value.relative_dir.parts[0]),
                episode_id=value.episode_id,
                source_session_id=value.source_session_id,
                collection_type=value.collection_type,
                outcome=value.outcome,
                task_id=value.metadata_task_id,
                task_description=value.metadata_task_description,
                mcap=value.mcap,
                metadata=value.metadata,
            )
        )
    return DiscoveryResult(source_root, (), tuple(candidates), ())


def _job_counts(run_dir: Path) -> dict[str, int]:
    latest = {}
    with (run_dir / "jobs.jsonl").open() as stream:
        for line in stream:
            value = json.loads(line)
            latest[value["episode_id"]] = value["state"]
    result: dict[str, int] = {}
    for state in latest.values():
        result[state] = result.get(state, 0) + 1
    return dict(sorted(result.items()))


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _phase_summary(run_dir: Path) -> dict[str, dict[str, float | int]]:
    observations: dict[str, list[float]] = defaultdict(list)
    path = run_dir / "metrics.jsonl"
    if not path.is_file():
        return {}
    with path.open() as stream:
        for line in stream:
            value = json.loads(line)
            if value.get("kind") != "work_completed":
                continue
            observations[value["phase"]].append(float(value["elapsed_seconds"]))
            for name, seconds in value.get("phase_seconds", {}).items():
                observations[f"convert.{name}"].append(float(seconds))
    return {
        phase: {
            "count": len(values),
            "total": sum(values),
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
            "max": max(values),
        }
        for phase, values in sorted(observations.items())
    }


def _video_bytes(output: Path) -> int:
    return sum(path.stat().st_size for path in output.rglob("*.mp4"))


def run_e2e(args: argparse.Namespace) -> int:
    entries = _read_manifest(args.manifest)
    source_config_path = args.config.resolve(strict=True)
    config = StreamingConversionConfig.load(source_config_path)
    runtime = config.runtime
    if not isinstance(runtime, (PrefetchRuntimeConfig, BufferedRuntimeConfig)):
        raise ValueError("benchmark e2e requires a PFS-contained runtime")
    benchmark_root = _inside(args.benchmark_root, runtime.pfs_root, "benchmark root")
    benchmark_root.mkdir(parents=True, exist_ok=True)
    run_id = _run_id(args.label)
    recipe_path = args.recipe_profile.resolve(strict=True)
    runtime = replace(runtime, streaming_root=benchmark_root / "work")
    config = replace(
        config,
        runtime=runtime,
        recipe=replace(config.recipe, profile=str(recipe_path)),
    )
    recipe = load_conversion_recipe(source_config_path, config)
    output = benchmark_root / "data" / run_id
    manifest = RunManifest.create(
        config,
        _discovery(config.source.root.resolve(strict=True), entries),
        output,
        run_id,
        repo_id=config.output.repo_id_for(output),
    )
    before = _resources(args.cgroup_root)
    started = time.monotonic()
    execution_log = io.StringIO()
    result = None
    error = None
    with ResourceSampler(args.cgroup_root) as sampler:
        try:
            result = execute_frozen_streaming_run(
                config,
                recipe,
                manifest,
                execution_log,
            )
        except Exception as exception:
            error = f"{type(exception).__name__}: {exception}"
            execution_log.write(traceback.format_exc())
    elapsed = time.monotonic() - started
    after = _resources(args.cgroup_root)
    states = _job_counts(manifest.run_dir)
    accepted = (
        error is None
        and states.get("failed", 0) == 0
        and states.get("discarded", 0) == 0
        and states.get("committed", 0) + states.get("excluded", 0) == len(entries)
    )
    metric = {
        "schema_version": 2,
        "phase": "streaming_e2e",
        "run_id": run_id,
        "recipe_profile": str(recipe_path),
        "video": recipe.video.as_report() if recipe.video is not None else None,
        "episodes": len(entries),
        "source_bytes": sum(entry.source_bytes for entry in entries),
        "elapsed_seconds": elapsed,
        "source_gb_s": (
            sum(entry.source_bytes for entry in entries) / 1_000_000_000 / elapsed
        ),
        "frames": result.snapshot.frame_count if result is not None else 0,
        "frames_s": result.snapshot.frame_count / elapsed if result is not None else 0.0,
        "video_bytes": _video_bytes(output),
        "states": states,
        "phase_seconds": _phase_summary(manifest.run_dir),
        "resource": sampler.summary(),
        "cpu_delta": _cpu_delta(before, after),
        "accepted": accepted,
        "error": error,
        "output": str(output),
    }
    evidence = benchmark_root / "results" / run_id
    _write_json(evidence / "metrics.json", metric)
    (evidence / "execution.log").write_text(execution_log.getvalue())
    shutil.copy2(source_config_path, evidence / "streaming-config.toml")
    shutil.copy2(recipe_path, evidence / "recipe.toml")
    print(json.dumps(metric, sort_keys=True), flush=True)
    if args.cleanup_data and accepted:
        _remove_isolated(output, benchmark_root)
        _remove_isolated(manifest.run_dir, benchmark_root)
    return 0 if accepted else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    sample = commands.add_parser("sample")
    sample.set_defaults(handler=run_sample)
    sample.add_argument("--manifest", type=Path, required=True)
    sample.add_argument("--output", type=Path, required=True)
    choice = sample.add_mutually_exclusive_group(required=True)
    choice.add_argument("--count", type=int)
    choice.add_argument("--episode-id", action="append")

    stage = commands.add_parser("stage")
    stage.set_defaults(handler=run_stage)
    stage.add_argument("--manifest", type=Path, required=True)
    stage.add_argument("--source-root", type=Path, required=True)
    stage.add_argument("--containment-root", type=Path, required=True)
    stage.add_argument("--benchmark-root", type=Path, required=True)
    stage.add_argument("--workers", type=_workers, required=True)
    stage.add_argument("--label", required=True)
    stage.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))

    e2e = commands.add_parser("e2e")
    e2e.set_defaults(handler=run_e2e)
    e2e.add_argument("--manifest", type=Path, required=True)
    e2e.add_argument("--config", type=Path, required=True)
    e2e.add_argument("--recipe-profile", type=Path, required=True)
    e2e.add_argument("--benchmark-root", type=Path, required=True)
    e2e.add_argument("--label", required=True)
    e2e.add_argument("--cleanup-data", action="store_true")
    e2e.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
