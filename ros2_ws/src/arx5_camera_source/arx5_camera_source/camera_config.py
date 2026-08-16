from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CAMERA_ROLES = ("left", "right", "overview")


@dataclass(frozen=True)
class CameraSpec:
    role: str
    serial: str

    @property
    def namespace(self) -> str:
        return f"/sensors/camera_{self.role}"


def _camera_serial(role: str, value: Any) -> str:
    if isinstance(value, str):
        serial = value
    elif isinstance(value, dict):
        serial = value.get("serial", "")
    else:
        serial = ""
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError(f"camera {role!r} must define a non-empty serial")
    return serial.strip()


def load_station_cameras(path: Path) -> tuple[CameraSpec, ...]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read station config {path}: {error}") from error

    cameras = payload.get("cameras")
    if not isinstance(cameras, dict):
        raise ValueError("station config must contain a cameras object")

    missing = [role for role in CAMERA_ROLES if role not in cameras]
    extra = sorted(set(cameras) - set(CAMERA_ROLES))
    if missing or extra:
        raise ValueError(f"camera roles mismatch: missing={missing}, extra={extra}")

    specs = tuple(
        CameraSpec(role=role, serial=_camera_serial(role, cameras[role]))
        for role in CAMERA_ROLES
    )
    serials = [spec.serial for spec in specs]
    if len(set(serials)) != len(serials):
        raise ValueError("camera serials must be unique")
    return specs
