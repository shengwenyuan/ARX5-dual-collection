from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import time
import tomllib
from typing import Callable, Iterator, TextIO

from arx5_collection.adapters.bos.bucketlink import BaiduBucketLinkClient
from arx5_collection.adapters.bos.bucketlink import BucketLinkClient
from arx5_collection.adapters.bos.bucketlink import BucketLinkSpec
from arx5_collection.adapters.bos.bucketlink import BucketLinkStatus
from arx5_collection.adapters.bos.bucketlink import mounted_report_path
from arx5_collection.adapters.bos.bucketlink import validate_report
from arx5_collection.adapters.bos.bucketlink import wait_for_bucket_link

from ..application import DatasetPipelineRequest
from ..application import DatasetPipelineResult
from ..application import execute_dataset_pipeline_config
from ..configuration.run import BufferedRuntimeConfig
from ..configuration.run import DatasetPipelineConfig
from ..configuration.run import PrefetchRuntimeConfig
from .confirmation import require_enter_confirmation


@dataclass(frozen=True, slots=True)
class BucketLinkRunRequest:
    config_path: Path
    output_override: Path | None
    run_id: str | None
    resume_run_id: str | None
    retry_failed: bool = False


def execute_bucketlink_conversion(
    request: BucketLinkRunRequest,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    client: BucketLinkClient | None = None,
    report_reader: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DatasetPipelineResult:
    run_id = _run_id(request)
    bucketlink_name = _bucketlink_name(run_id)
    spec, conversion = load_bucketlink_config(request.config_path)
    run_dir = conversion.runtime.streaming_root / run_id
    journal = conversion.runtime.streaming_root / ".bucketlink" / f"{run_id}.json"
    resuming = request.resume_run_id is not None

    if run_dir.exists() and request.output_override is not None:
        raise ValueError("--output cannot change a resumed conversion")
    if not run_dir.exists() and request.output_override is None:
        raise ValueError("--output is required until the conversion manifest exists")
    if request.retry_failed and not run_dir.exists():
        raise ValueError("--retry-failed requires an existing conversion manifest")
    if request.output_override is not None and request.output_override.exists():
        raise FileExistsError(request.output_override)

    if not resuming:
        if journal.exists() or run_dir.exists():
            raise FileExistsError(f"run already exists: {run_id}")
        _render_transfer_alignment(spec, conversion, run_id, output_stream)
        require_enter_confirmation(input_stream, output_stream)
        _prepare_empty_target(spec, conversion)

    api = client or BaiduBucketLinkClient.from_default_credentials(spec.endpoint)
    status = wait_for_bucket_link(
        spec,
        bucketlink_name,
        journal,
        api,
        sleep=sleep,
        progress=lambda value: _render_progress(value, output_stream),
        create_missing=not resuming,
    )
    reader = report_reader or (lambda uri: _mounted_report_reader(uri, sleep))
    report = validate_report(reader(status.report or ""))
    output_stream.write(
        json.dumps({"bucketlink_report": report}, sort_keys=True) + "\n"
    )
    output_stream.flush()

    downstream = DatasetPipelineRequest(
        config_path=request.config_path,
        output_override=None if run_dir.exists() else request.output_override,
        run_id=None if run_dir.exists() else run_id,
        resume_run_id=run_id if run_dir.exists() else None,
        retry_failed=request.retry_failed,
    )
    with _conversion_slot(conversion, output_stream):
        return execute_dataset_pipeline_config(
            conversion, downstream, input_stream, output_stream
        )


def load_bucketlink_config(
    path: Path,
) -> tuple[BucketLinkSpec, DatasetPipelineConfig]:
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    expected = {"schema_version", "bucketlink", "source", "runtime", "output", "recipe"}
    if set(payload) != expected:
        raise ValueError(f"BucketLink config keys must be exactly {sorted(expected)}")
    bucketlink = payload.pop("bucketlink")
    if payload.get("schema_version") != 1:
        raise ValueError("BucketLink config schema_version must be 1")
    if not isinstance(bucketlink, dict):
        raise ValueError("BucketLink config must contain a [bucketlink] table")
    source = payload.get("source")
    if not isinstance(source, dict) or "materialization" in source:
        raise ValueError("BucketLink [source] must omit fixed materialization")
    source["materialization"] = "direct"

    runtime = payload.get("runtime")
    runtime_keys = {
        "pfs_root",
        "streaming_root",
        "conversion_workers",
        "temporary_hard_max_bytes",
        "min_free_bytes",
    }
    if not isinstance(runtime, dict) or set(runtime) != runtime_keys:
        raise ValueError(
            f"BucketLink runtime keys must be exactly {sorted(runtime_keys)}"
        )
    workers = runtime["conversion_workers"]
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("runtime.conversion_workers must be a positive integer")
    payload["schema_version"] = 3
    payload["runtime"] = {
        **runtime,
        "stage_workers": min(workers, 32),
        "ready_low_bytes": 1,
        "ready_high_bytes": 2,
        "max_staged_episodes": workers * 2,
    }

    spec = BucketLinkSpec.from_mapping(bucketlink)
    conversion = DatasetPipelineConfig.from_mapping(payload)
    _validate_target_mapping(spec, conversion)
    return spec, conversion


def _validate_target_mapping(
    spec: BucketLinkSpec, config: DatasetPipelineConfig
) -> None:
    runtime = config.runtime
    if not isinstance(runtime, (PrefetchRuntimeConfig, BufferedRuntimeConfig)):
        raise ValueError("BucketLink conversion requires a PFS runtime")
    if len(config.source.include_paths) != 1:
        raise ValueError(
            "one BucketLink batch requires exactly one source.include_paths entry"
        )
    include_target = config.source.root / config.source.include_paths[0]
    mounted_target = Path(spec.mounted_path)
    if mounted_target.resolve(strict=False) != include_target.resolve(strict=False):
        raise ValueError(
            "bucketlink.mounted_path must equal the configured source include path"
        )
    if (
        runtime.pfs_root.resolve(strict=False)
        not in mounted_target.resolve(strict=False).parents
    ):
        raise ValueError("bucketlink.mounted_path must be below runtime.pfs_root")


def _prepare_empty_target(spec: BucketLinkSpec, config: DatasetPipelineConfig) -> None:
    target = Path(spec.mounted_path)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"BucketLink target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)


def _run_id(request: BucketLinkRunRequest) -> str:
    if (request.run_id is None) == (request.resume_run_id is None):
        raise ValueError("exactly one of --run-id or --resume is required")
    value = request.run_id or request.resume_run_id
    assert value is not None
    if value in {".", ".."} or Path(value).name != value:
        raise ValueError("run identity must be one path component")
    if len(value) > 128:
        raise ValueError("run identity exceeds the BucketLink name limit")
    if request.retry_failed and request.resume_run_id is None:
        raise ValueError("--retry-failed requires --resume")
    return value


def _bucketlink_name(run_id: str) -> str:
    name = f"arx5-{run_id}"
    if len(name) > 128:
        raise ValueError("run identity is too long for the BucketLink name")
    return name


def _render_transfer_alignment(
    spec: BucketLinkSpec,
    config: DatasetPipelineConfig,
    run_id: str,
    output_stream: TextIO,
) -> None:
    output_stream.write(
        "ARX5 BucketLink transfer alignment\n"
        f"run_id: {run_id}\n"
        f"bucketlink_name: {_bucketlink_name(run_id)}\n"
        f"source: bos://{spec.bucket}/{spec.bucket_prefix}\n"
        f"destination: {spec.pfs_path}\n"
        f"mounted_source: {config.source.root / config.source.include_paths[0]}\n"
        f"throughput_limit_bytes: {spec.throughput_limit_bytes}\n"
    )
    output_stream.flush()


def _render_progress(status: BucketLinkStatus, output_stream: TextIO) -> None:
    output_stream.write(
        json.dumps(
            {
                "bucketlink_progress": {
                    "status": status.status,
                    "progress": status.progress,
                }
            },
            sort_keys=True,
        )
        + "\n"
    )
    output_stream.flush()


@contextmanager
def _conversion_slot(
    config: DatasetPipelineConfig, output_stream: TextIO
) -> Iterator[None]:
    runtime = config.runtime
    assert isinstance(runtime, (PrefetchRuntimeConfig, BufferedRuntimeConfig))
    path = runtime.pfs_root / ".arx5-bucketlink-conversion.lock"
    output_stream.write(json.dumps({"conversion_slot": "waiting"}) + "\n")
    output_stream.flush()
    with path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        output_stream.write(json.dumps({"conversion_slot": "acquired"}) + "\n")
        output_stream.flush()
        yield


def _mounted_report_reader(
    report_uri: str,
    sleep: Callable[[float], None],
    *,
    timeout_seconds: float = 300.0,
) -> str:
    mount_root = Path(os.environ.get("ARX5_BOS_MOUNT_ROOT", "/mnt/bos"))
    path = mounted_report_path(report_uri, mount_root)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return path.read_text()
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise
            sleep(5.0)
