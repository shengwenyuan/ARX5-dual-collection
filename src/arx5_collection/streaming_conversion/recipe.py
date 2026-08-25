from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from arx5_collection.cleaning.models import CleaningPolicy
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy


@dataclass(frozen=True, slots=True)
class Pi05ConversionRecipe:
    schema_version: int
    name: str
    builder_backend: str
    cleaning: CleaningPolicy
    selection: EqualEefPolicy
    left_gripper: GripperCalibration
    right_gripper: GripperCalibration

    @classmethod
    def load(cls, path: str | Path) -> Pi05ConversionRecipe:
        with Path(path).open("rb") as stream:
            payload = tomllib.load(stream)
        _exact_keys(
            payload,
            {
                "schema_version",
                "name",
                "builder_backend",
                "cleaning",
                "selection",
                "gripper",
            },
            "conversion recipe",
        )
        if payload["schema_version"] != 1:
            raise ValueError("conversion recipe schema_version must be 1")
        name = _string(payload["name"], "name")
        if name != "pi05-equal-eef-v2":
            raise ValueError("unsupported conversion recipe name")
        backend = _string(payload["builder_backend"], "builder_backend")
        if backend != "lerobot-v2.1":
            raise ValueError("unsupported conversion builder_backend")

        cleaning = _table(payload, "cleaning")
        selection = _table(payload, "selection")
        gripper = _table(payload, "gripper")
        left = _table(gripper, "left")
        right = _table(gripper, "right")
        _exact_keys(
            cleaning,
            {
                "cross_camera_tolerance_ns",
                "arm_max_age_ns",
                "camera_gap_warning_ns",
                "arm_gap_warning_ns",
                "grade_a_coverage",
                "grade_b_coverage",
            },
            "cleaning",
        )
        _exact_keys(
            selection,
            {
                "eef_distance_m",
                "gripper_delta_threshold",
                "max_sample_interval_ns",
                "image_max_age_ns",
                "arm_max_age_ns",
                "action_horizon",
                "nominal_fps",
                "idle_delta_threshold",
                "min_idle_frames",
                "min_motion_frames",
                "trim_segment_end_frames",
                "max_episode_duration_s",
            },
            "selection",
        )
        _exact_keys(left, {"open_value", "closed_value", "tolerance"}, "gripper.left")
        _exact_keys(
            right,
            {"open_value", "closed_value", "tolerance"},
            "gripper.right",
        )
        return cls(
            schema_version=1,
            name=name,
            builder_backend=backend,
            cleaning=CleaningPolicy(
                cross_camera_tolerance_ns=_int(
                    cleaning["cross_camera_tolerance_ns"],
                    "cleaning.cross_camera_tolerance_ns",
                ),
                arm_max_age_ns=_int(
                    cleaning["arm_max_age_ns"], "cleaning.arm_max_age_ns"
                ),
                camera_gap_warning_ns=_int(
                    cleaning["camera_gap_warning_ns"],
                    "cleaning.camera_gap_warning_ns",
                ),
                arm_gap_warning_ns=_int(
                    cleaning["arm_gap_warning_ns"],
                    "cleaning.arm_gap_warning_ns",
                ),
                grade_a_coverage=_number(
                    cleaning["grade_a_coverage"], "cleaning.grade_a_coverage"
                ),
                grade_b_coverage=_number(
                    cleaning["grade_b_coverage"], "cleaning.grade_b_coverage"
                ),
            ),
            selection=EqualEefPolicy(
                eef_distance_m=_number(
                    selection["eef_distance_m"], "selection.eef_distance_m"
                ),
                gripper_delta_threshold=_number(
                    selection["gripper_delta_threshold"],
                    "selection.gripper_delta_threshold",
                ),
                max_sample_interval_ns=_int(
                    selection["max_sample_interval_ns"],
                    "selection.max_sample_interval_ns",
                ),
                image_max_age_ns=_int(
                    selection["image_max_age_ns"], "selection.image_max_age_ns"
                ),
                arm_max_age_ns=_int(
                    selection["arm_max_age_ns"], "selection.arm_max_age_ns"
                ),
                action_horizon=_int(
                    selection["action_horizon"], "selection.action_horizon"
                ),
                nominal_fps=_int(
                    selection["nominal_fps"], "selection.nominal_fps"
                ),
                idle_delta_threshold=_number(
                    selection["idle_delta_threshold"],
                    "selection.idle_delta_threshold",
                ),
                min_idle_frames=_int(
                    selection["min_idle_frames"], "selection.min_idle_frames"
                ),
                min_motion_frames=_int(
                    selection["min_motion_frames"], "selection.min_motion_frames"
                ),
                trim_segment_end_frames=_int(
                    selection["trim_segment_end_frames"],
                    "selection.trim_segment_end_frames",
                ),
                max_episode_duration_s=_number(
                    selection["max_episode_duration_s"],
                    "selection.max_episode_duration_s",
                ),
            ),
            left_gripper=_gripper(left, "gripper.left"),
            right_gripper=_gripper(right, "gripper.right"),
        )


def _gripper(payload: dict[str, object], label: str) -> GripperCalibration:
    return GripperCalibration(
        open_value=_number(payload["open_value"], f"{label}.open_value"),
        closed_value=_number(payload["closed_value"], f"{label}.closed_value"),
        tolerance=_number(payload["tolerance"], f"{label}.tolerance"),
    )


def _table(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"conversion recipe must contain a [{name}] table")
    return value


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} keys must be exactly {sorted(expected)}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    return float(value)
