"""Bounded quintic joint motion and measured stationarity, independent of ROS."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import numpy as np


@dataclass(frozen=True)
class MotionLimits:
    velocity_rad_s: float = 0.10
    acceleration_rad_s2: float = 0.20
    rate_hz: float = 50.0
    arrival_rad: float = 0.01
    still_velocity_rad_s: float = 0.01
    stable_s: float = 0.6
    capture_s: float = 2.0
    feedback_age_s: float = 0.10
    following_error_rad: float = 0.08
    segment_timeout_s: float = 120.0

    def __post_init__(self):
        if any(
            isinstance(v, bool) or not math.isfinite(v) or v <= 0
            for v in asdict(self).values()
        ):
            raise ValueError("motion limits must be positive finite numbers")
        if self.velocity_rad_s > 0.20 or self.acceleration_rad_s2 > 0.40:
            raise ValueError("calibration speed ceiling: 0.20 rad/s, 0.40 rad/s²")
        if not 25 <= self.rate_hz <= 100 or self.feedback_age_s > 0.15:
            raise ValueError("invalid control rate or feedback freshness")
        if (
            self.arrival_rad > 0.02
            or self.still_velocity_rad_s > 0.02
            or self.following_error_rad > 0.10
        ):
            raise ValueError("motion tolerances too loose")
        if self.stable_s < 0.5 or self.capture_s < 1 or self.segment_timeout_s > 180:
            raise ValueError("invalid dwell/segment limits")


class Segment:
    """q(s)=q0+(q1-q0)(10s³-15s⁴+6s⁵), zero end velocity/acceleration."""

    def __init__(self, start, end, limits: MotionLimits):
        self.start, self.end = np.array(start, float), np.array(end, float)
        if (
            self.start.shape != (6,)
            or self.end.shape != (6,)
            or not np.isfinite([self.start, self.end]).all()
        ):
            raise ValueError("invalid segment endpoints")
        distance = float(np.max(np.abs(self.end - self.start)))
        self.duration = max(
            0.5,
            1.875 * distance / limits.velocity_rad_s,
            math.sqrt((10 / math.sqrt(3)) * distance / limits.acceleration_rad_s2),
        )
        if (
            self.duration + limits.stable_s + limits.capture_s + 5
            > limits.segment_timeout_s
        ):
            raise ValueError("segment exceeds time budget; teach an intermediate point")

    def at(self, elapsed_s):
        s = np.clip(elapsed_s / self.duration, 0, 1)
        blend = 10 * s**3 - 15 * s**4 + 6 * s**5
        return self.start + (self.end - self.start) * blend


def vector(value, size=6):
    a = np.asarray(value, float)
    if a.shape != (size,) or not np.isfinite(a).all():
        raise ValueError(f"expected {size} finite values")
    return a


def validate_sample(sample: dict, now: float, limits: MotionLimits) -> None:
    for side in ("left", "right"):
        arm = sample[side]
        vector(arm["q"])
        vector(arm["velocity"])
        if not math.isfinite(arm["gripper"]):
            raise ValueError("invalid gripper state")
        age = now - arm["received_monotonic_s"]
        if not math.isfinite(age) or age < 0 or age > limits.feedback_age_s:
            raise RuntimeError("stale arm feedback")


def stationary(sample, reference, limits):
    return all(
        np.max(np.abs(vector(sample[s]["q"]) - vector(reference[s])))
        <= limits.arrival_rad
        and np.max(np.abs(vector(sample[s]["velocity"]))) <= limits.still_velocity_rad_s
        for s in ("left", "right")
    )
