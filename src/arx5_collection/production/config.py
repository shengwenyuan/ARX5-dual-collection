from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARM_ROLES = ("left", "right")
CAMERA_ROLES = ("left", "right", "overview")
TRIGGER_ROLES = ("activate", "abort")
EXPECTED_STREAMS = {
    "left_arm_state": "/embodiments/left_arm/state",
    "right_arm_state": "/embodiments/right_arm/state",
    "camera_left_color": "/sensors/camera_left/color/image_raw",
    "camera_left_aligned_depth": "/sensors/camera_left/aligned_depth/image_raw",
    "camera_right_color": "/sensors/camera_right/color/image_raw",
    "camera_right_aligned_depth": "/sensors/camera_right/aligned_depth/image_raw",
    "camera_overview_color": "/sensors/camera_overview/color/image_raw",
    "camera_overview_aligned_depth": "/sensors/camera_overview/aligned_depth/image_raw",
}


@dataclass(frozen=True, slots=True)
class ArmConfig:
    role: str
    usb_serial: str
    can_interface: str


@dataclass(frozen=True, slots=True)
class CameraConfig:
    role: str
    serial_number: str


@dataclass(frozen=True, slots=True)
class PedalConfig:
    role: str
    vendor_id: str
    product_id: str
    serial_number: str


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    activate: PedalConfig
    abort: PedalConfig


@dataclass(frozen=True, slots=True)
class StationConfig:
    schema_version: int
    station_id: str
    sdk_type: int
    arms: tuple[ArmConfig, ...]
    cameras: tuple[CameraConfig, ...]
    triggers: TriggerConfig | None = None

    def metadata(self) -> dict[str, Any]:
        devices = [
            {
                "id": f"{arm.role}_arm",
                "kind": "arm",
                "serial_number": arm.usb_serial,
                "configuration": {
                    "can_interface": arm.can_interface,
                    "sdk_type": self.sdk_type,
                },
            }
            for arm in self.arms
        ]
        devices.extend(
            {
                "id": f"camera_{camera.role}",
                "kind": "camera",
                "serial_number": camera.serial_number,
                "configuration": {},
            }
            for camera in self.cameras
        )
        return {
            "id": self.station_id,
            "config_schema_version": self.schema_version,
            "devices": devices,
        }


def load_station_config(path: Path) -> StationConfig:
    payload = _load_object(path, "station")
    schema_version = payload.get("schema_version")
    if schema_version == 1:
        _require_exact_keys(
            payload,
            {"schema_version", "station_id", "sdk_type", "arms", "cameras"},
            "station",
        )
    elif schema_version == 2:
        _require_exact_keys(
            payload,
            {
                "schema_version",
                "station_id",
                "sdk_type",
                "arms",
                "cameras",
                "triggers",
            },
            "station",
        )
    else:
        raise ValueError("station schema_version must be 1 or 2")
    station_id = _non_empty_string(payload["station_id"], "station_id")
    sdk_type = payload["sdk_type"]
    if sdk_type not in {1, 2}:
        raise ValueError("sdk_type must be 1 or 2")

    arms_value = payload["arms"]
    if not isinstance(arms_value, list):
        raise ValueError("arms must be an array")
    arms_by_role: dict[str, ArmConfig] = {}
    for value in arms_value:
        _require_exact_keys(value, {"name", "usb_serial", "can_interface"}, "arm")
        role = _non_empty_string(value["name"], "arm name")
        if role not in ARM_ROLES or role in arms_by_role:
            raise ValueError(f"arm roles must be exactly {list(ARM_ROLES)}")
        arms_by_role[role] = ArmConfig(
            role=role,
            usb_serial=_non_empty_string(value["usb_serial"], f"arm {role} usb_serial"),
            can_interface=_non_empty_string(
                value["can_interface"], f"arm {role} can_interface"
            ),
        )
    if set(arms_by_role) != set(ARM_ROLES):
        raise ValueError(f"arm roles must be exactly {list(ARM_ROLES)}")

    cameras_value = payload["cameras"]
    _require_exact_keys(cameras_value, set(CAMERA_ROLES), "cameras")
    cameras = tuple(
        CameraConfig(
            role=role,
            serial_number=_camera_serial(cameras_value[role], role),
        )
        for role in CAMERA_ROLES
    )

    triggers = None
    if schema_version == 2:
        triggers = _trigger_config(payload["triggers"])

    all_serials = [arm.usb_serial for arm in arms_by_role.values()]
    all_serials.extend(camera.serial_number for camera in cameras)
    if len(all_serials) != len(set(all_serials)):
        raise ValueError("station device serial numbers must be unique")
    can_interfaces = [arm.can_interface for arm in arms_by_role.values()]
    if len(can_interfaces) != len(set(can_interfaces)):
        raise ValueError("arm CAN interfaces must be unique")

    return StationConfig(
        schema_version=schema_version,
        station_id=station_id,
        sdk_type=sdk_type,
        arms=tuple(arms_by_role[role] for role in ARM_ROLES),
        cameras=cameras,
        triggers=triggers,
    )


def validate_task_streams(path: Path) -> None:
    payload = _load_object(path, "task")
    _require_exact_keys(payload, {"task_id", "task_description", "streams"}, "task")
    streams = payload["streams"]
    if not isinstance(streams, list):
        raise ValueError("streams must be an array")
    actual: dict[str, str] = {}
    for stream in streams:
        _require_exact_keys(
            stream,
            {"id", "topic", "required", "expected_hz"},
            "stream",
        )
        stream_id = _non_empty_string(stream["id"], "stream id")
        topic = _non_empty_string(stream["topic"], f"stream {stream_id} topic")
        if stream.get("required") is not True:
            raise ValueError(f"production stream {stream_id} must be required")
        if stream_id in actual:
            raise ValueError("stream ids must be unique")
        actual[stream_id] = topic
    if actual != EXPECTED_STREAMS:
        raise ValueError("production task must contain the fixed eight-stream contract")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label} config {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} config must be an object")
    return payload


def _require_exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} keys must be exactly {sorted(expected)}")


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _camera_serial(value: object, role: str) -> str:
    if isinstance(value, str):
        serial = value
    elif isinstance(value, dict) and set(value) == {"serial_number"}:
        serial = value["serial_number"]
    else:
        raise ValueError(
            f"camera {role} must be a serial string or serial_number object"
        )
    return _non_empty_string(serial, f"camera {role} serial_number")


def _trigger_config(value: object) -> TriggerConfig:
    _require_exact_keys(value, set(TRIGGER_ROLES), "triggers")
    assert isinstance(value, dict)
    pedals: dict[str, PedalConfig] = {}
    for role in TRIGGER_ROLES:
        pedal = value[role]
        _require_exact_keys(
            pedal,
            {
                "vendor_id",
                "product_id",
                "serial_number",
            },
            f"trigger {role}",
        )
        assert isinstance(pedal, dict)
        pedals[role] = PedalConfig(
            role=role,
            vendor_id=_usb_hex_id(pedal["vendor_id"], f"trigger {role} vendor_id"),
            product_id=_usb_hex_id(pedal["product_id"], f"trigger {role} product_id"),
            serial_number=_non_empty_string(
                pedal["serial_number"], f"trigger {role} serial_number"
            ),
        )
    if pedals["activate"].serial_number == pedals["abort"].serial_number:
        raise ValueError("trigger pedals must use different serial numbers")
    return TriggerConfig(activate=pedals["activate"], abort=pedals["abort"])


def _usb_hex_id(value: object, label: str) -> str:
    normalized = _non_empty_string(value, label).lower()
    if len(normalized) != 4 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be four hexadecimal characters")
    return normalized
