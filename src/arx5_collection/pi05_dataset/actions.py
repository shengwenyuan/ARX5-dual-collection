from __future__ import annotations

from dataclasses import dataclass

from arx5_collection.cleaning.models import ArmSample


@dataclass(frozen=True, slots=True)
class GripperCalibration:
    open_value: float
    closed_value: float
    tolerance: float = 0.05

    def __post_init__(self) -> None:
        if self.open_value == self.closed_value:
            raise ValueError("gripper open and closed values must differ")
        if self.tolerance < 0:
            raise ValueError("gripper tolerance must not be negative")

    def normalize(self, value: float) -> float:
        normalized = (value - self.open_value) / (self.closed_value - self.open_value)
        if normalized < -self.tolerance or normalized > 1.0 + self.tolerance:
            raise ValueError(
                f"gripper value {value} maps outside calibrated range: {normalized}"
            )
        return min(1.0, max(0.0, normalized))


def make_state(
    left: ArmSample,
    right: ArmSample,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
) -> tuple[float, ...]:
    return (
        *left.joint_positions,
        left_gripper.normalize(left.gripper_position),
        *right.joint_positions,
        right_gripper.normalize(right.gripper_position),
    )
