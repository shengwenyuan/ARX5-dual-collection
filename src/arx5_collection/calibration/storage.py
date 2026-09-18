"""Portable, versioned calibration files with atomic draft writes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

ROLES = {
    "left-wrist": ("left", "left", "eye_in_hand"),
    "right-wrist": ("right", "right", "eye_in_hand"),
    "overview-left": ("overview", "left", "eye_to_hand"),
    "overview-right": ("overview", "right", "eye_to_hand"),
}
DEFAULT_ROOT = Path("/var/lib/arx5-collection/calibration")


def identifier() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(
        path.read_text(),
        object_pairs_hook=unique,
        parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def route_path(root: Path, role: str) -> Path:
    return root / "routes" / f"{role}.json"


def archive_route(path: Path) -> None:
    if path.exists():
        backup = path.parent / "history" / f"{path.stem}-{identifier()}.json"
        write_json(backup, read_json(path))


def bound_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("evidence path escapes its run directory")
    return path
