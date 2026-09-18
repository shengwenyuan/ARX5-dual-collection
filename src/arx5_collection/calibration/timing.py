"""One timing contract for live capture and immutable offline evidence."""

import math

MAX_RECEIPT_AGE_S = 0.25
MIN_SOURCE_AGE_S = -0.02
MAX_SOURCE_AGE_S = 0.20


def timing_error(frame):
    if frame.get("clock_error"):
        return frame["clock_error"]
    if frame.get("clock") != "global_time":
        return "waiting for camera global time"
    try:
        source, wall, received, mono = (
            frame[k]
            for k in (
                "source_time_s",
                "received_wall_s",
                "received_monotonic_s",
                "source_monotonic_s",
            )
        )
        if not all(math.isfinite(v) for v in (source, wall, received, mono)):
            return "invalid camera timestamp"
        age = wall - source
        if not MIN_SOURCE_AGE_S <= age <= MAX_SOURCE_AGE_S:
            return "camera clock settling / frame outside time bounds"
        if abs(mono - (received - age)) > 1e-6:
            return "inconsistent camera clock mapping"
    except (KeyError, TypeError, ValueError):
        return "invalid camera timestamp"
    return ""


def capture_ready(frame, now):
    return not timing_error(frame) and (
        0 <= now - frame["received_monotonic_s"] <= MAX_RECEIPT_AGE_S
        and MIN_SOURCE_AGE_S <= now - frame["source_monotonic_s"] <= MAX_RECEIPT_AGE_S
    )
