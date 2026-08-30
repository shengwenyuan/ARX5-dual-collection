from __future__ import annotations

from dataclasses import dataclass
import math


ARX5_GRIPPER_CONTRACT_ID = "arx5-gripper-v1"
ARX5_GRIPPER_OPEN_RAW = -3.4
ARX5_GRIPPER_CLOSED_RAW = 0.0
ARX5_GRIPPER_OPEN_TOLERANCE = 0.05
ARX5_GRIPPER_CLOSED_TOLERANCE = 0.10


@dataclass(frozen=True, slots=True)
class GripperCalibration:
    open_value: float
    closed_value: float
    open_tolerance: float = ARX5_GRIPPER_OPEN_TOLERANCE
    closed_tolerance: float = ARX5_GRIPPER_CLOSED_TOLERANCE

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.open_value,
                self.closed_value,
                self.open_tolerance,
                self.closed_tolerance,
            )
        ):
            raise ValueError("gripper calibration values must be finite")
        if self.open_value == self.closed_value:
            raise ValueError("gripper open and closed values must differ")
        if self.open_tolerance < 0 or self.closed_tolerance < 0:
            raise ValueError("gripper tolerances must not be negative")

    def normalize(self, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("gripper position must be finite")
        normalized = (value - self.open_value) / (
            self.closed_value - self.open_value
        )
        if (
            normalized < -self.open_tolerance
            or normalized > 1.0 + self.closed_tolerance
        ):
            raise ValueError(
                f"gripper value {value} maps outside calibrated range: {normalized}"
            )
        return min(1.0, max(0.0, normalized))

    def denormalize(self, value: float) -> float:
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("normalized gripper value must be within [0, 1]")
        return self.extrapolate(value)

    def extrapolate(self, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("normalized gripper value must be finite")
        return self.open_value + value * (self.closed_value - self.open_value)


ARX5_GRIPPER_CALIBRATION = GripperCalibration(
    open_value=ARX5_GRIPPER_OPEN_RAW,
    closed_value=ARX5_GRIPPER_CLOSED_RAW,
)
