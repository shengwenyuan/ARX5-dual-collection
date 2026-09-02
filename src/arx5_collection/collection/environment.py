from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from arx5_collection.config import environment_config_path


@dataclass(frozen=True, slots=True)
class EnvironmentPaths:
    station_config: Path
    station_log_dir: Path
    device_root: Path
    hidraw_sysfs_root: Path
    usb_sysfs_root: Path
    usbfs_parameter: Path
    mcap_cli: Path
    can_state_dir: Path
    snapshot_arena_template: str
    snapshot_socket_template: str


@dataclass(frozen=True, slots=True)
class SystemSettings:
    usbfs_memory_mb: int
    can_startup_timeout_s: float
    can_speed: str
    mcap_cli_version: str


@dataclass(frozen=True, slots=True)
class StationSettings:
    sdk_type: int
    arm_roles: tuple[str, ...]
    camera_roles: tuple[str, ...]
    camera_binding_order: tuple[str, ...]
    trigger_roles: tuple[str, ...]
    provisional_can_interfaces: tuple[str, ...]
    movement_threshold_rad: float
    movement_timeout_s: float
    pedal_timeout_s: float


@dataclass(frozen=True, slots=True)
class CameraSettings:
    width: int
    height: int
    fps: int
    color_format: str
    depth_format: str
    frame_timeout_ms: int
    reliability: str
    camera_history_size: int
    arm_history_size: int


@dataclass(frozen=True, slots=True)
class SessionSettings:
    min_free_gib: int
    readiness_timeout_s: float
    fail_directory: str
    dagger_fail_directory: str
    compression_enabled: bool
    poll_interval_s: float


@dataclass(frozen=True, slots=True)
class MonitorSettings:
    display_period_s: float
    dagger_display_period_s: float
    startup_grace_s: float
    heartbeat_timeout_s: float
    data_silence_timeout_s: float
    warning_ratio: float


@dataclass(frozen=True, slots=True)
class RecordingSettings:
    start_timeout_s: float
    stop_timeout_s: float
    warning_ratio: float


@dataclass(frozen=True, slots=True)
class ProcessSettings:
    interrupt_timeout_s: float
    terminate_timeout_s: float
    kill_timeout_s: float


@dataclass(frozen=True, slots=True)
class ResetSettings:
    home: tuple[float, ...]
    timeout_s: float
    position_tolerance_rad: float
    velocity_tolerance_rad_s: float
    stable_s: float


@dataclass(frozen=True, slots=True)
class TriggerSettings:
    keyboard_activate: str
    keyboard_abort: str
    keyboard_ownership: str
    pedal_debounce_s: float


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    paths: EnvironmentPaths
    system: SystemSettings
    station: StationSettings
    camera: CameraSettings
    session: SessionSettings
    monitor: MonitorSettings
    recording: RecordingSettings
    process: ProcessSettings
    reset: ResetSettings
    trigger: TriggerSettings

    @classmethod
    def load(cls, path: Path) -> EnvironmentConfig:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        _exact(
            value,
            {
                "schema_version",
                "paths",
                "system",
                "station",
                "camera",
                "session",
                "monitor",
                "recording",
                "process",
                "reset",
                "trigger",
            },
            "environment",
        )
        if value["schema_version"] != 1:
            raise ValueError("environment schema_version must be 1")
        paths = _table(
            value,
            "paths",
            {
                "station_config",
                "station_log_dir",
                "device_root",
                "hidraw_sysfs_root",
                "usb_sysfs_root",
                "usbfs_parameter",
                "mcap_cli",
                "can_state_dir",
                "snapshot_arena_template",
                "snapshot_socket_template",
            },
        )
        system = _table(
            value,
            "system",
            {
                "usbfs_memory_mb",
                "can_startup_timeout_s",
                "can_speed",
                "mcap_cli_version",
            },
        )
        station = _table(
            value,
            "station",
            {
                "sdk_type",
                "arm_roles",
                "camera_roles",
                "camera_binding_order",
                "trigger_roles",
                "provisional_can_interfaces",
                "movement_threshold_rad",
                "movement_timeout_s",
                "pedal_timeout_s",
            },
        )
        camera = _table(
            value,
            "camera",
            {
                "width",
                "height",
                "fps",
                "color_format",
                "depth_format",
                "frame_timeout_ms",
                "reliability",
                "camera_history_size",
                "arm_history_size",
            },
        )
        session = _table(
            value,
            "session",
            {
                "min_free_gib",
                "readiness_timeout_s",
                "fail_directory",
                "dagger_fail_directory",
                "compression_enabled",
                "poll_interval_s",
            },
        )
        monitor = _table(
            value,
            "monitor",
            {
                "display_period_s",
                "dagger_display_period_s",
                "startup_grace_s",
                "heartbeat_timeout_s",
                "data_silence_timeout_s",
                "warning_ratio",
            },
        )
        recording = _table(
            value, "recording", {"start_timeout_s", "stop_timeout_s", "warning_ratio"}
        )
        process = _table(
            value,
            "process",
            {"interrupt_timeout_s", "terminate_timeout_s", "kill_timeout_s"},
        )
        reset = _table(
            value,
            "reset",
            {
                "home",
                "timeout_s",
                "position_tolerance_rad",
                "velocity_tolerance_rad_s",
                "stable_s",
            },
        )
        trigger = _table(
            value,
            "trigger",
            {
                "keyboard_activate",
                "keyboard_abort",
                "keyboard_ownership",
                "pedal_debounce_s",
            },
        )
        result = cls(
            paths=EnvironmentPaths(
                **{
                    key: _absolute_path(paths[key], f"paths.{key}")
                    for key in (
                        "station_config",
                        "station_log_dir",
                        "device_root",
                        "hidraw_sysfs_root",
                        "usb_sysfs_root",
                        "usbfs_parameter",
                        "mcap_cli",
                        "can_state_dir",
                    )
                },
                snapshot_arena_template=_absolute_template(
                    paths["snapshot_arena_template"],
                    "paths.snapshot_arena_template",
                ),
                snapshot_socket_template=_absolute_template(
                    paths["snapshot_socket_template"],
                    "paths.snapshot_socket_template",
                ),
            ),
            system=SystemSettings(
                _positive_int(system["usbfs_memory_mb"], "system.usbfs_memory_mb"),
                _positive_number(
                    system["can_startup_timeout_s"], "system.can_startup_timeout_s"
                ),
                _text(system["can_speed"], "system.can_speed"),
                _text(system["mcap_cli_version"], "system.mcap_cli_version"),
            ),
            station=StationSettings(
                _positive_int(station["sdk_type"], "station.sdk_type"),
                _strings(station["arm_roles"], "station.arm_roles"),
                _strings(station["camera_roles"], "station.camera_roles"),
                _strings(
                    station["camera_binding_order"], "station.camera_binding_order"
                ),
                _strings(station["trigger_roles"], "station.trigger_roles"),
                _strings(
                    station["provisional_can_interfaces"],
                    "station.provisional_can_interfaces",
                ),
                _positive_number(
                    station["movement_threshold_rad"], "station.movement_threshold_rad"
                ),
                _positive_number(
                    station["movement_timeout_s"], "station.movement_timeout_s"
                ),
                _positive_number(station["pedal_timeout_s"], "station.pedal_timeout_s"),
            ),
            camera=CameraSettings(
                _positive_int(camera["width"], "camera.width"),
                _positive_int(camera["height"], "camera.height"),
                _positive_int(camera["fps"], "camera.fps"),
                _text(camera["color_format"], "camera.color_format"),
                _text(camera["depth_format"], "camera.depth_format"),
                _positive_int(camera["frame_timeout_ms"], "camera.frame_timeout_ms"),
                _text(camera["reliability"], "camera.reliability"),
                _positive_int(
                    camera["camera_history_size"], "camera.camera_history_size"
                ),
                _positive_int(camera["arm_history_size"], "camera.arm_history_size"),
            ),
            session=SessionSettings(
                _positive_int(session["min_free_gib"], "session.min_free_gib"),
                _positive_number(
                    session["readiness_timeout_s"], "session.readiness_timeout_s"
                ),
                _text(session["fail_directory"], "session.fail_directory"),
                _text(
                    session["dagger_fail_directory"], "session.dagger_fail_directory"
                ),
                _boolean(session["compression_enabled"], "session.compression_enabled"),
                _positive_number(session["poll_interval_s"], "session.poll_interval_s"),
            ),
            monitor=MonitorSettings(
                _positive_number(
                    monitor["display_period_s"], "monitor.display_period_s"
                ),
                _positive_number(
                    monitor["dagger_display_period_s"],
                    "monitor.dagger_display_period_s",
                ),
                _positive_number(monitor["startup_grace_s"], "monitor.startup_grace_s"),
                _positive_number(
                    monitor["heartbeat_timeout_s"], "monitor.heartbeat_timeout_s"
                ),
                _positive_number(
                    monitor["data_silence_timeout_s"], "monitor.data_silence_timeout_s"
                ),
                _ratio(monitor["warning_ratio"], "monitor.warning_ratio"),
            ),
            recording=RecordingSettings(
                _positive_number(
                    recording["start_timeout_s"], "recording.start_timeout_s"
                ),
                _positive_number(
                    recording["stop_timeout_s"], "recording.stop_timeout_s"
                ),
                _ratio(recording["warning_ratio"], "recording.warning_ratio"),
            ),
            process=ProcessSettings(
                _positive_number(
                    process["interrupt_timeout_s"], "process.interrupt_timeout_s"
                ),
                _positive_number(
                    process["terminate_timeout_s"], "process.terminate_timeout_s"
                ),
                _positive_number(process["kill_timeout_s"], "process.kill_timeout_s"),
            ),
            reset=ResetSettings(
                tuple(
                    _number(item, "reset.home")
                    for item in _list(reset["home"], "reset.home")
                ),
                _positive_number(reset["timeout_s"], "reset.timeout_s"),
                _positive_number(
                    reset["position_tolerance_rad"], "reset.position_tolerance_rad"
                ),
                _positive_number(
                    reset["velocity_tolerance_rad_s"], "reset.velocity_tolerance_rad_s"
                ),
                _positive_number(reset["stable_s"], "reset.stable_s"),
            ),
            trigger=TriggerSettings(
                _character(trigger["keyboard_activate"], "trigger.keyboard_activate"),
                _character(trigger["keyboard_abort"], "trigger.keyboard_abort"),
                _character(trigger["keyboard_ownership"], "trigger.keyboard_ownership"),
                _positive_number(
                    trigger["pedal_debounce_s"], "trigger.pedal_debounce_s"
                ),
            ),
        )
        if set(result.station.camera_binding_order) != set(result.station.camera_roles):
            raise ValueError(
                "station.camera_binding_order must contain every camera role"
            )
        if len(result.station.arm_roles) != len(
            result.station.provisional_can_interfaces
        ):
            raise ValueError(
                "station arm roles and provisional CAN interfaces must align"
            )
        if len(result.reset.home) != 6:
            raise ValueError("reset.home must contain six joints")
        if result.station.sdk_type not in {1, 2}:
            raise ValueError("station.sdk_type must be 1 or 2")
        if result.camera.reliability not in {"best_effort", "reliable"}:
            raise ValueError("camera.reliability must be best_effort or reliable")
        if re.fullmatch(r"s[0-9]+", result.system.can_speed) is None:
            raise ValueError("system.can_speed must use slcand sN syntax")
        keys = (
            result.trigger.keyboard_activate,
            result.trigger.keyboard_abort,
            result.trigger.keyboard_ownership,
        )
        if len(set(keys)) != len(keys):
            raise ValueError("trigger keyboard keys must be unique")
        return result


def _table(value: dict[str, object], name: str, keys: set[str]) -> dict[str, object]:
    table = value[name]
    _exact(table, keys, name)
    return table


def _exact(value: object, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} keys must be exactly {sorted(keys)}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _character(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 1:
        raise ValueError(f"{label} must be one character")
    return text


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_text(value, label))
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


def _absolute_template(value: object, label: str) -> str:
    template = _text(value, label)
    if not template.startswith("/") or template.count("{ros_domain_id}") != 1:
        raise ValueError(f"{label} must be absolute and contain ros_domain_id")
    try:
        template.format(ros_domain_id=0)
    except (KeyError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    return template


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    return float(value)


def _positive_number(value: object, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _ratio(value: object, label: str) -> float:
    result = _number(value, label)
    if not 0 < result <= 1:
        raise ValueError(f"{label} must be in (0, 1]")
    return result


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty array")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    result = tuple(_text(item, label) for item in _list(value, label))
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must contain unique values")
    return result


ENVIRONMENT = EnvironmentConfig.load(environment_config_path())
