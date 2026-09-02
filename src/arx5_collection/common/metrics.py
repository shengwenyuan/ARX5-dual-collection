from __future__ import annotations

import math
from collections.abc import Iterable


def finite_scalar(value: object, field: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"{field} must be finite")
    return result


def finite_vector(values: Iterable[object], width: int, field: str) -> list[float]:
    result = [float(value) for value in values]
    if len(result) != width or not all(math.isfinite(value) for value in result):
        raise RuntimeError(f"{field} must contain {width} finite values")
    return result


def split_arm_feedback(
    values: Iterable[object], field: str
) -> tuple[list[float], float]:
    """Split the Vendor SDK's [J1..J6, gripper] feedback vector."""
    result = finite_vector(values, 7, field)
    return result[:6], result[6]


def timing_summary(timestamps_ns: list[int]) -> dict[str, float | int]:
    if not timestamps_ns:
        return {"count": 0, "duration_s": 0.0, "observed_hz": 0.0, "max_gap_ms": 0.0}
    if len(timestamps_ns) == 1:
        return {"count": 1, "duration_s": 0.0, "observed_hz": 0.0, "max_gap_ms": 0.0}
    gaps = [right - left for left, right in zip(timestamps_ns, timestamps_ns[1:])]
    duration_s = (timestamps_ns[-1] - timestamps_ns[0]) / 1e9
    observed_hz = (len(timestamps_ns) - 1) / duration_s if duration_s > 0 else 0.0
    return {
        "count": len(timestamps_ns),
        "duration_s": duration_s,
        "observed_hz": observed_hz,
        "max_gap_ms": max(gaps) / 1e6,
    }
