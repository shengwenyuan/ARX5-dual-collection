from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Protocol

from arx5_collection.atomic import staged_directory

from .models import EpisodeCandidate
from .models import FileIdentity
from .models import StageReceipt


STAGE_SCHEMA_VERSION = 2
_MCAP_NAME = "episode.mcap"
_METADATA_NAME = "metadata.json"


class SourceChangedError(RuntimeError):
    pass


class StageValidationError(RuntimeError):
    pass


class EpisodeSource(Protocol):
    def stage(self, candidate: EpisodeCandidate, target: Path) -> StageReceipt: ...


class MountedEpisodeSource:
    def __init__(self, source_root: Path) -> None:
        self._source_root = source_root.resolve(strict=True)
        if not self._source_root.is_dir():
            raise NotADirectoryError(self._source_root)

    def stage(self, candidate: EpisodeCandidate, target: Path) -> StageReceipt:
        if not target.is_absolute():
            raise ValueError("staging target must be an absolute path")
        source_dir = self._inside_root(candidate.source_dir)
        if source_dir.name != candidate.episode_id:
            raise ValueError("source directory name does not match Episode ID")
        source_mcap = self._inside_root(source_dir / _MCAP_NAME)
        source_metadata = self._inside_root(source_dir / _METADATA_NAME)
        self._assert_source_identity(source_mcap, candidate.mcap)
        self._assert_source_identity(source_metadata, candidate.metadata)

        with staged_directory(target) as temporary:
            _copy_file(source_mcap, temporary / _MCAP_NAME)
            _copy_file(source_metadata, temporary / _METADATA_NAME)
            self._assert_source_identity(source_mcap, candidate.mcap)
            self._assert_source_identity(source_metadata, candidate.metadata)
            receipt = StageReceipt(
                episode_id=candidate.episode_id,
                source_session_id=candidate.source_session_id,
                source_dir=source_dir,
                stage_dir=target,
                mcap=candidate.mcap,
                metadata=candidate.metadata,
            )
            _validate_staged_files(receipt, temporary)
            _write_stage_manifest(temporary / "stage.json", receipt)

        return validate_stage(target)

    def _inside_root(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise SourceChangedError(
                f"source changed after confirmation: missing path: {path}"
            ) from error
        try:
            resolved.relative_to(self._source_root)
        except ValueError as error:
            raise ValueError(
                f"mounted Episode source escapes configured root: {path} -> {resolved}"
            ) from error
        return resolved

    @staticmethod
    def _assert_source_identity(path: Path, expected: FileIdentity) -> None:
        observed = _identity(path)
        if observed != expected:
            raise SourceChangedError(
                f"source changed after confirmation: {path}: "
                f"expected={expected}, observed={observed}"
            )


def validate_stage(stage_dir: Path) -> StageReceipt:
    manifest_path = stage_dir / "stage.json"
    try:
        value = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StageValidationError(f"cannot read stage manifest: {manifest_path}") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "episode_id",
        "source_session_id",
        "source_dir",
        "mcap",
        "metadata",
    }:
        raise StageValidationError("invalid stage manifest keys")
    if value["schema_version"] != STAGE_SCHEMA_VERSION:
        raise StageValidationError("unsupported stage manifest schema_version")
    receipt = StageReceipt(
        episode_id=_string(value["episode_id"], "episode_id"),
        source_session_id=_string(value["source_session_id"], "source_session_id"),
        source_dir=_absolute_path(value["source_dir"], "source_dir"),
        stage_dir=stage_dir,
        mcap=_manifest_identity(value["mcap"], "mcap"),
        metadata=_manifest_identity(value["metadata"], "metadata"),
    )
    _validate_staged_files(receipt, stage_dir)
    return receipt


def _copy_file(source: Path, target: Path) -> None:
    with source.open("rb") as input_stream, target.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=8 * 1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())


def _validate_staged_files(receipt: StageReceipt, stage_dir: Path) -> None:
    expected_sizes = {
        _MCAP_NAME: receipt.mcap.size,
        _METADATA_NAME: receipt.metadata.size,
    }
    for name, expected_size in expected_sizes.items():
        path = stage_dir / name
        try:
            observed_size = path.stat().st_size
        except OSError as error:
            raise StageValidationError(f"missing staged file: {path}") from error
        if not path.is_file() or observed_size != expected_size:
            raise StageValidationError(
                f"staged file size mismatch: {path}: "
                f"expected={expected_size}, observed={observed_size}"
            )


def _write_stage_manifest(path: Path, receipt: StageReceipt) -> None:
    value = {
        "schema_version": STAGE_SCHEMA_VERSION,
        "episode_id": receipt.episode_id,
        "source_session_id": receipt.source_session_id,
        "source_dir": str(receipt.source_dir),
        "mcap": _identity_value(receipt.mcap),
        "metadata": _identity_value(receipt.metadata),
    }
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _identity(path: Path) -> FileIdentity:
    stat = path.stat()
    return FileIdentity(stat.st_size, stat.st_mtime_ns)


def _identity_value(identity: FileIdentity) -> dict[str, int]:
    return {"size": identity.size, "mtime_ns": identity.mtime_ns}


def _manifest_identity(value: object, label: str) -> FileIdentity:
    if not isinstance(value, dict) or set(value) != {"size", "mtime_ns"}:
        raise StageValidationError(f"invalid {label} identity")
    return FileIdentity(
        _non_negative_int(value["size"], f"{label}.size"),
        _non_negative_int(value["mtime_ns"], f"{label}.mtime_ns"),
    )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StageValidationError(f"{label} must be a non-empty string")
    return value


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_string(value, label))
    if not path.is_absolute():
        raise StageValidationError(f"{label} must be an absolute path")
    return path


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StageValidationError(f"{label} must be a non-negative integer")
    return value
