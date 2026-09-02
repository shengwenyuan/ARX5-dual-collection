from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Protocol


PFS_THROUGHPUT_LIMITS = {629_145_600, 1_258_291_200, 1_572_864_000}
TERMINAL_FAILURES = {3: "failed", 5: "cancelled", 6: "deleting", 8: "paused"}


@dataclass(frozen=True, slots=True)
class BucketLinkSpec:
    endpoint: str
    instance_id: str
    bucket: str
    bucket_prefix: str
    pfs_path: str
    mounted_path: str
    throughput_limit_bytes: int
    conflict_policy: int
    report_prefix: str

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> BucketLinkSpec:
        expected = {
            "endpoint",
            "instance_id",
            "bucket",
            "bucket_prefix",
            "pfs_path",
            "mounted_path",
            "throughput_limit_bytes",
            "conflict_policy",
            "report_prefix",
        }
        if set(value) != expected:
            raise ValueError(f"bucketlink keys must be exactly {sorted(expected)}")
        spec = cls(
            endpoint=_text(value["endpoint"], "bucketlink.endpoint"),
            instance_id=_text(value["instance_id"], "bucketlink.instance_id"),
            bucket=_text(value["bucket"], "bucketlink.bucket"),
            bucket_prefix=_text(value["bucket_prefix"], "bucketlink.bucket_prefix"),
            pfs_path=_text(value["pfs_path"], "bucketlink.pfs_path"),
            mounted_path=_text(value["mounted_path"], "bucketlink.mounted_path"),
            throughput_limit_bytes=_integer(
                value["throughput_limit_bytes"], "bucketlink.throughput_limit_bytes"
            ),
            conflict_policy=_integer(
                value["conflict_policy"], "bucketlink.conflict_policy"
            ),
            report_prefix=_text(value["report_prefix"], "bucketlink.report_prefix"),
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if self.bucket_prefix.startswith("/") or not self.bucket_prefix.endswith("/"):
            raise ValueError(
                "bucketlink.bucket_prefix must not start with '/' and must end with '/'"
            )
        path = Path(self.pfs_path)
        if not path.is_absolute() or self.pfs_path.endswith("/") or ".." in path.parts:
            raise ValueError(
                "bucketlink.pfs_path must be a normalized absolute path without trailing '/'"
            )
        if not Path(self.mounted_path).is_absolute():
            raise ValueError("bucketlink.mounted_path must be absolute")
        if self.throughput_limit_bytes not in PFS_THROUGHPUT_LIMITS:
            raise ValueError("bucketlink.throughput_limit_bytes is not a supported PFS limit")
        if self.conflict_policy not in {1, 2, 3}:
            raise ValueError("bucketlink.conflict_policy must be 1, 2, or 3")


@dataclass(frozen=True, slots=True)
class BucketLinkCreated:
    bucket_link_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class BucketLinkStatus:
    status: int
    progress: int | None = None
    report: str | None = None
    error: str | None = None
    source: str | None = None
    destination: str | None = None


class BucketLinkClient(Protocol):
    def create(self, spec: BucketLinkSpec, name: str) -> BucketLinkCreated: ...

    def describe(self, instance_id: str, bucket_link_id: str) -> BucketLinkStatus: ...


class BaiduBucketLinkClient:
    """Thin adapter around Baidu's generated PFS SDK."""

    def __init__(self, client: object, models: object) -> None:
        self._client = client
        self._models = models

    @classmethod
    def from_environment(cls, endpoint: str) -> BaiduBucketLinkClient:
        try:
            from baiducloud_python_sdk_core.auth.bce_credentials import BceCredentials
            from baiducloud_python_sdk_core.bce_client_configuration import BceClientConfiguration
            from baiducloud_python_sdk_pfs import models
            from baiducloud_python_sdk_pfs.api.pfs_client import PfsClient
        except ImportError as error:
            raise RuntimeError(
                "BucketLink requires the 'bucketlink' optional dependency"
            ) from error
        access_key = _secret("BCE_ACCESS_KEY_ID")
        secret_key = _secret("BCE_SECRET_ACCESS_KEY")
        credentials = BceCredentials(access_key, secret_key)
        config = BceClientConfiguration(credentials=credentials, endpoint=endpoint)
        return cls(PfsClient(config), models)

    def create(self, spec: BucketLinkSpec, name: str) -> BucketLinkCreated:
        request = self._models.CreateL2BucketLinkRequest(
            instance_id=spec.instance_id,
            conflict_policy=spec.conflict_policy,
            bucket_name=spec.bucket,
            bucket_prefix=spec.bucket_prefix,
            throughput_limit_bytes=spec.throughput_limit_bytes,
            report_object_name=spec.report_prefix,
            bucket_link_name=name,
            transfer_type=1,
            pfs_path=spec.pfs_path,
            scope=2,
        )
        response = self._client.create_l2_bucket_link(request)
        return BucketLinkCreated(response.bucket_link_id, response.request_id)

    def describe(self, instance_id: str, bucket_link_id: str) -> BucketLinkStatus:
        request = self._models.DescL2BucketLinkRequest(
            instance_id=instance_id,
            bucket_link_id=bucket_link_id,
        )
        response = self._client.desc_l2_bucket_link(request)
        return BucketLinkStatus(
            status=response.status,
            progress=getattr(response, "progress", None),
            report=getattr(response, "report", None),
            error=getattr(response, "errmsg", None),
            source=getattr(response, "src", None),
            destination=getattr(response, "dst", None),
        )


def wait_for_bucket_link(
    spec: BucketLinkSpec,
    name: str,
    journal_path: Path,
    client: BucketLinkClient,
    *,
    poll_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[BucketLinkStatus], None] | None = None,
    create_missing: bool = True,
) -> BucketLinkStatus:
    created = _load_created(journal_path, spec, name)
    if created is None:
        if not create_missing:
            raise FileNotFoundError(f"BucketLink journal not found: {journal_path}")
        created = client.create(spec, name)
        _write_journal(journal_path, spec, name, created)

    while True:
        status = client.describe(spec.instance_id, created.bucket_link_id)
        _write_status(journal_path, status)
        if progress is not None:
            progress(status)
        if status.status == 2:
            if not status.report:
                raise RuntimeError("successful BucketLink has no task report")
            expected_source = f"bos://{spec.bucket}/{spec.bucket_prefix}"
            if status.source != expected_source:
                raise RuntimeError("BucketLink source differs from the frozen request")
            if status.destination != spec.pfs_path:
                raise RuntimeError("BucketLink destination differs from the frozen request")
            return status
        if reason := TERMINAL_FAILURES.get(status.status):
            detail = f": {status.error}" if status.error else ""
            raise RuntimeError(f"BucketLink {reason}{detail}")
        sleep(poll_seconds)


def validate_report(text: str) -> dict[str, int]:
    counts = {
        key: int(match.group(1))
        for key in ("totalCount", "skippedCount", "failedCount")
        if (match := re.search(rf"^{key}:\s*(\d+)\s*$", text, re.MULTILINE))
    }
    if set(counts) != {"totalCount", "skippedCount", "failedCount"}:
        raise ValueError("BucketLink report is missing required summary counts")
    if counts["failedCount"]:
        raise RuntimeError(f"BucketLink report contains {counts['failedCount']} failed files")
    if counts["skippedCount"]:
        raise RuntimeError(f"BucketLink report contains {counts['skippedCount']} skipped files")
    if counts["totalCount"] == 0:
        raise RuntimeError("BucketLink report contains no files")
    return counts


def mounted_report_path(report_uri: str, mount_root: Path) -> Path:
    prefix = "bos://"
    if not report_uri.startswith(prefix):
        raise ValueError(f"unsupported BucketLink report URI: {report_uri}")
    relative = Path(report_uri.removeprefix(prefix))
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2:
        raise ValueError(f"unsafe BucketLink report URI: {report_uri}")
    return mount_root.joinpath(*relative.parts)


def _load_created(
    path: Path, spec: BucketLinkSpec, name: str
) -> BucketLinkCreated | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if payload.get("spec") != asdict(spec) or payload.get("name") != name:
        raise ValueError("BucketLink journal differs from the requested batch")
    return BucketLinkCreated(payload["bucket_link_id"], payload["request_id"])


def _write_journal(
    path: Path, spec: BucketLinkSpec, name: str, created: BucketLinkCreated
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "name": name,
        "spec": asdict(spec),
        **asdict(created),
    }
    _write_json(path, payload)


def _write_status(path: Path, status: BucketLinkStatus) -> None:
    payload = json.loads(path.read_text())
    payload["last_status"] = asdict(status)
    _write_json(path, payload)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value
