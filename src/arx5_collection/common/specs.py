from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from arx5_collection.config import config_path


@dataclass(frozen=True, slots=True)
class GripperSpec:
    contract_id: str
    open_value: float
    closed_value: float
    open_tolerance: float
    closed_tolerance: float


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    fps: int
    action_horizon: int
    sequential_execution_steps: int
    control_rate_hz: float
    model_action_dimension: int
    image_width: int
    image_height: int
    camera_keys: tuple[str, ...]
    motor_names: tuple[str, ...]
    openpi_commit: str
    lerobot_commit: str
    lerobot_v3_version: str
    lerobot_v3_commit: str


@dataclass(frozen=True, slots=True)
class Pi05Arx5Spec:
    gripper: GripperSpec
    dataset: DatasetSpec

    @classmethod
    def load(cls) -> Pi05Arx5Spec:
        with config_path("specs/pi05-arx5.toml").open("rb") as stream:
            value = tomllib.load(stream)
        if (
            set(value) != {"schema_version", "contract_id", "gripper", "dataset"}
            or value["schema_version"] != 1
        ):
            raise ValueError(
                "pi05 ARX5 spec must use schema_version 1 and exact top-level keys"
            )
        gripper = value["gripper"]
        dataset = value["dataset"]
        if not isinstance(gripper, dict) or set(gripper) != {
            "open_value",
            "closed_value",
            "open_tolerance",
            "closed_tolerance",
        }:
            raise ValueError("pi05 ARX5 gripper spec keys are invalid")
        if not isinstance(dataset, dict) or set(dataset) != {
            "fps",
            "action_horizon",
            "sequential_execution_steps",
            "control_rate_hz",
            "model_action_dimension",
            "image_width",
            "image_height",
            "camera_keys",
            "motor_names",
            "openpi_commit",
            "lerobot_commit",
            "lerobot_v3_version",
            "lerobot_v3_commit",
        }:
            raise ValueError("pi05 ARX5 dataset spec keys are invalid")
        result = cls(
            GripperSpec(
                _text(value["contract_id"], "contract_id"),
                _number(gripper["open_value"], "gripper.open_value"),
                _number(gripper["closed_value"], "gripper.closed_value"),
                _positive_number(gripper["open_tolerance"], "gripper.open_tolerance"),
                _positive_number(
                    gripper["closed_tolerance"], "gripper.closed_tolerance"
                ),
            ),
            DatasetSpec(
                _positive_int(dataset["fps"], "dataset.fps"),
                _positive_int(dataset["action_horizon"], "dataset.action_horizon"),
                _positive_int(
                    dataset["sequential_execution_steps"],
                    "dataset.sequential_execution_steps",
                ),
                _positive_number(dataset["control_rate_hz"], "dataset.control_rate_hz"),
                _positive_int(
                    dataset["model_action_dimension"],
                    "dataset.model_action_dimension",
                ),
                _positive_int(dataset["image_width"], "dataset.image_width"),
                _positive_int(dataset["image_height"], "dataset.image_height"),
                _strings(dataset["camera_keys"], "dataset.camera_keys"),
                _strings(dataset["motor_names"], "dataset.motor_names"),
                _commit(dataset["openpi_commit"], "dataset.openpi_commit"),
                _commit(dataset["lerobot_commit"], "dataset.lerobot_commit"),
                _text(dataset["lerobot_v3_version"], "dataset.lerobot_v3_version"),
                _commit(dataset["lerobot_v3_commit"], "dataset.lerobot_v3_commit"),
            ),
        )
        if result.gripper.open_value == result.gripper.closed_value:
            raise ValueError("gripper open and closed values must differ")
        return result


@dataclass(frozen=True, slots=True)
class RuntimeInterfaceSpec:
    stream_status_topic: str
    stream_status_message_type: str
    stream_status_period_s: float

    @classmethod
    def load(cls, path: Path | None = None) -> RuntimeInterfaceSpec:
        source = path or config_path("specs/runtime-interface.toml")
        with source.open("rb") as stream:
            value = tomllib.load(stream)
        if (
            set(value)
            != {
                "schema_version",
                "stream_status_topic",
                "stream_status_message_type",
                "stream_status_period_s",
            }
            or value["schema_version"] != 1
        ):
            raise ValueError(
                "runtime interface spec must use schema_version 1 and exact keys"
            )
        result = cls(
            _text(value["stream_status_topic"], "stream_status_topic"),
            _text(
                value["stream_status_message_type"],
                "stream_status_message_type",
            ),
            _positive_number(
                value["stream_status_period_s"],
                "stream_status_period_s",
            ),
        )
        if not result.stream_status_topic.startswith("/"):
            raise ValueError("stream_status_topic must be absolute")
        return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


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


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty array")
    result = tuple(_text(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must contain unique values")
    return result


def _commit(value: object, label: str) -> str:
    text = _text(value, label)
    if re.fullmatch(r"[0-9a-f]{40}", text) is None:
        raise ValueError(f"{label} must be a full Git commit")
    return text


PI05_ARX5_SPEC = Pi05Arx5Spec.load()
RUNTIME_INTERFACE_SPEC = RuntimeInterfaceSpec.load()
