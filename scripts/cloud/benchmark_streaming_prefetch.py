#!/usr/bin/env python3
"""Run isolated BOS staging and bounded-prefetch end-to-end benchmarks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import subprocess
import sys
import threading
import time

from arx5_collection.streaming_conversion.coordinator import StageWork, stage_episode
from arx5_collection.streaming_conversion.models import FileIdentity, SelectionEntry


GIB = 1024**3
PFS_ROOT = Path("/mnt/pfs/swy")
MEMORY_WORKING_SET_LIMIT = 220 * GIB


def _selection(path: Path) -> list[SelectionEntry]:
    result: list[SelectionEntry] = []
    with path.open() as stream:
        for line in stream:
            value = json.loads(line)
            result.append(
                SelectionEntry(
                    episode_id=value["episode_id"],
                    source_session_id=value["source_session_id"],
                    source_dir=Path(value["source_dir"]),
                    relative_dir=Path(value["relative_dir"]),
                    collection_type=value["collection_type"],
                    outcome=value["outcome"],
                    metadata_task_id=value["metadata_task"]["id"],
                    metadata_task_description=value["metadata_task"]["description"],
                    training_task=value["training_task"],
                    mcap=FileIdentity(**value["mcap"]),
                    metadata=FileIdentity(**value["metadata"]),
                )
            )
    return result


def _inside_pfs(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if resolved == PFS_ROOT or PFS_ROOT not in resolved.parents:
        raise ValueError(f"benchmark path must be below {PFS_ROOT}: {path}")
    return resolved


def _read_int(path: str) -> int:
    return int(Path(path).read_text().strip())


def _memory() -> dict[str, int]:
    stat = {}
    with Path("/sys/fs/cgroup/memory.stat").open() as stream:
        for line in stream:
            key, value = line.split()
            stat[key] = int(value)
    return {
        "current": _read_int("/sys/fs/cgroup/memory.current"),
        "anon": stat.get("anon", 0),
        "file": stat.get("file", 0),
        "kernel": stat.get("kernel", 0),
        "working_set": stat.get("anon", 0) + stat.get("kernel", 0),
    }


def _cpu_stat() -> dict[str, int]:
    result = {}
    with Path("/sys/fs/cgroup/cpu.stat").open() as stream:
        for line in stream:
            key, value = line.split()
            result[key] = int(value)
    return result


class ResourceSampler:
    def __init__(self, *, process_group: int | None = None) -> None:
        self.process_group = process_group
        self.stop = threading.Event()
        self.samples: list[dict[str, object]] = []
        self.safety_stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> ResourceSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self._thread.join()

    def _run(self) -> None:
        over_limit = 0
        while not self.stop.wait(2.0):
            memory = _memory()
            self.samples.append(
                {
                    "elapsed_s": time.monotonic(),
                    "memory": memory,
                    "cpu": _cpu_stat(),
                }
            )
            if memory["working_set"] >= MEMORY_WORKING_SET_LIMIT:
                over_limit += 1
            else:
                over_limit = 0
            if over_limit >= 3 and self.process_group is not None:
                self.safety_stop = True
                os.killpg(self.process_group, signal.SIGTERM)
                return

    def summary(self) -> dict[str, object]:
        memories = [sample["memory"] for sample in self.samples]
        return {
            "samples": len(self.samples),
            "peak_memory_current_bytes": max(
                (value["current"] for value in memories), default=0
            ),
            "peak_memory_working_set_bytes": max(
                (value["working_set"] for value in memories), default=0
            ),
            "peak_memory_anon_bytes": max(
                (value["anon"] for value in memories), default=0
            ),
            "peak_memory_file_bytes": max(
                (value["file"] for value in memories), default=0
            ),
            "safety_stop": self.safety_stop,
        }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _remove_benchmark_tree(path: Path, benchmark_root: Path) -> None:
    resolved = path.resolve(strict=False)
    root = benchmark_root.resolve(strict=True)
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"refusing cleanup outside benchmark root: {path}")
    if resolved.exists():
        shutil.rmtree(resolved)


def run_stage(args: argparse.Namespace) -> int:
    benchmark_root = _inside_pfs(args.benchmark_root)
    benchmark_root.mkdir(parents=True, exist_ok=True)
    entries = sorted(_selection(args.manifest), key=lambda item: item.mcap.size)[
        : args.sample_count
    ]
    sample = {
        "selection": "smallest_by_mcap_size",
        "episodes": [item.episode_id for item in entries],
        "bytes": sum(item.mcap.size + item.metadata.size for item in entries),
    }
    _write_json(benchmark_root / "metrics" / "stage-sample.json", sample)

    for workers in args.workers:
        target_root = benchmark_root / "work" / f"stage-w{workers}"
        if target_root.exists():
            raise FileExistsError(target_root)
        target_root.mkdir(parents=True)
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        cpu_before = _cpu_stat()
        errors = []
        with ResourceSampler() as sampler:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        stage_episode,
                        StageWork(args.source_root, entry, target_root / entry.episode_id),
                    ): entry.episode_id
                    for entry in entries
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:  # benchmark must retain all failures
                        errors.append(
                            {
                                "episode_id": futures[future],
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
        elapsed = time.monotonic() - started
        cpu_after = _cpu_stat()
        copied_bytes = sum(item.mcap.size + item.metadata.size for item in entries)
        metric = {
            "schema_version": 1,
            "phase": "bos_stage",
            "workers": workers,
            "episodes": len(entries),
            "bytes": copied_bytes,
            "elapsed_s": elapsed,
            "throughput_gb_s": copied_bytes / 1_000_000_000 / elapsed,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "errors": errors,
            "resource": sampler.summary(),
            "cpu_stat_delta": {
                key: cpu_after.get(key, 0) - cpu_before.get(key, 0)
                for key in cpu_after
            },
        }
        _write_json(benchmark_root / "metrics" / f"stage-w{workers}.json", metric)
        print(json.dumps(metric, sort_keys=True), flush=True)
        _remove_benchmark_tree(target_root, benchmark_root)
        if errors:
            return 2
    return 0


def _profile_text(
    entries: list[SelectionEntry],
    args: argparse.Namespace,
    workers: int,
) -> str:
    include_paths = ",\n".join(
        f"  {json.dumps(entry.relative_dir.as_posix())}" for entry in entries
    )
    return f'''schema_version = 2

[source]
root = {json.dumps(str(args.source_root))}
include_paths = [
{include_paths},
]
block = ["test", "tests", "abort", "aborted", "log", "logs"]

[runtime]
pfs_root = {json.dumps(str(PFS_ROOT))}
streaming_root = {json.dumps(str(args.benchmark_root / "runs"))}
stage_workers = {args.stage_workers}
conversion_workers = {workers}
prefetch_target_bytes = 1_500_000_000_000
prefetch_max_bytes = 2_000_000_000_000
prefetch_max_episodes = 128

[output]
lerobot_root = {json.dumps(str(args.benchmark_root / "outputs"))}
dataset_name = "throughput_benchmark"
repo_id = "local/throughput_benchmark"

[recipe]
name = "pi05-equal-eef-v3"
profile = {json.dumps(str(args.recipe_profile))}
task = "folding the cloth"
'''


def _job_counts(run_dir: Path) -> dict[str, int]:
    latest = {}
    with (run_dir / "jobs.jsonl").open() as stream:
        for line in stream:
            value = json.loads(line)
            latest[value["episode_id"]] = value["state"]
    result = {}
    for state in latest.values():
        result[state] = result.get(state, 0) + 1
    return dict(sorted(result.items()))


def run_e2e(args: argparse.Namespace) -> int:
    benchmark_root = _inside_pfs(args.benchmark_root)
    benchmark_root.mkdir(parents=True, exist_ok=True)
    entries = sorted(_selection(args.manifest), key=lambda item: item.mcap.size)[
        : args.sample_count
    ]
    sample_bytes = sum(item.mcap.size + item.metadata.size for item in entries)
    if sample_bytes > 2_000_000_000_000:
        raise ValueError(f"sample requires {sample_bytes} bytes, above 2 TB hard limit")
    _write_json(
        benchmark_root / "metrics" / "e2e-sample.json",
        {
            "selection": "smallest_by_mcap_size",
            "episodes": [item.episode_id for item in entries],
            "bytes": sample_bytes,
        },
    )

    for workers in args.workers:
        run_id = f"20260827-throughput-e2e-w{workers}"
        output_path = benchmark_root / "outputs" / f"throughput-e2e-w{workers}"
        run_dir = benchmark_root / "runs" / run_id
        profile = benchmark_root / "configs" / f"e2e-w{workers}.toml"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(_profile_text(entries, args, workers))
        if output_path.exists() or run_dir.exists():
            raise FileExistsError(f"benchmark tier already exists: {workers}")
        log_path = benchmark_root / "metrics" / f"e2e-w{workers}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(args.dataset_cli),
            "stream-to-lerobot",
            "--config",
            str(profile),
            "--run-id",
            run_id,
            "--output",
            str(output_path),
        ]
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        cpu_before = _cpu_stat()
        with log_path.open("w") as log:
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    command,
                    stdin=slave_fd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                os.close(slave_fd)
                slave_fd = -1
                os.write(master_fd, b"\n")
                with ResourceSampler(process_group=process.pid) as sampler:
                    return_code = process.wait()
            finally:
                if slave_fd >= 0:
                    os.close(slave_fd)
                os.close(master_fd)
        elapsed = time.monotonic() - started
        cpu_after = _cpu_stat()
        counts = _job_counts(run_dir) if run_dir.exists() else {}
        metric = {
            "schema_version": 1,
            "phase": "bounded_prefetch_e2e",
            "stage_workers": args.stage_workers,
            "conversion_workers": workers,
            "sample_episodes": len(entries),
            "sample_bytes": sample_bytes,
            "elapsed_s": elapsed,
            "terminal_episodes_per_hour": sum(
                counts.get(state, 0) for state in ("committed", "excluded")
            )
            * 3600
            / elapsed,
            "return_code": return_code,
            "states": counts,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "resource": sampler.summary(),
            "cpu_stat_delta": {
                key: cpu_after.get(key, 0) - cpu_before.get(key, 0)
                for key in cpu_after
            },
            "log": str(log_path),
        }
        _write_json(benchmark_root / "metrics" / f"e2e-w{workers}.json", metric)
        print(json.dumps(metric, sort_keys=True), flush=True)
        if return_code != 0 or sampler.safety_stop:
            return return_code or 3
        if args.cleanup:
            _remove_benchmark_tree(output_path, benchmark_root)
            _remove_benchmark_tree(run_dir, benchmark_root)
    return 0


def _workers(value: str) -> list[int]:
    result = [int(item) for item in value.split(",")]
    if not result or any(item < 1 for item in result):
        raise argparse.ArgumentTypeError("workers must be comma-separated positive ints")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("stage", run_stage), ("e2e", run_e2e)):
        child = subparsers.add_parser(name)
        child.set_defaults(handler=handler)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--source-root", type=Path, required=True)
        child.add_argument("--benchmark-root", type=Path, required=True)
        child.add_argument("--sample-count", type=int, required=True)
        child.add_argument("--workers", type=_workers, required=True)
    e2e = subparsers.choices["e2e"]
    e2e.add_argument("--stage-workers", type=int, required=True)
    e2e.add_argument("--recipe-profile", type=Path, required=True)
    e2e.add_argument("--dataset-cli", type=Path, required=True)
    e2e.add_argument("--cleanup", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.sample_count < max(args.workers):
        raise SystemExit("sample-count must be at least the largest worker tier")
    args.benchmark_root = _inside_pfs(args.benchmark_root)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
