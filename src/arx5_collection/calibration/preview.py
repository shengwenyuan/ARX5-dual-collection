"""Independent validation rows beside the preview; raw evidence stays untouched."""

from dataclasses import dataclass

import cv2
import numpy as np

from .board import overlay
from .motion import Segment
from .timing import capture_ready, timing_error

ROLE_NAMES = {
    "left-wrist": "Left wrist",
    "right-wrist": "Right wrist",
    "overview-left": "Overview / left arm",
    "overview-right": "Overview / right arm",
}
WINDOW_CLOSED = -2


@dataclass(frozen=True)
class Check:
    label: str
    value: str
    passed: bool


def live_checks(frame, detection, board, state, kin, limits, gate, stable, now):
    found = detection.corners is not None
    height, width = frame["image"].shape[:2]
    margin = 0.0 if not found else float(min(
        detection.corners.min(),
        (width - detection.corners[:, 0]).min(),
        (height - detection.corners[:, 1]).min(),
    ))
    checks = [
        Check("Corners", f"{board.columns} x {board.rows}" if found else "not fully detected", found),
        Check("Coverage", f"{detection.coverage:.1%} / min {board.min_coverage:.1%}", found and detection.coverage >= board.min_coverage),
        Check("Sharpness", f"{detection.sharpness:.0f} / min {board.min_sharpness:g}", found and detection.sharpness >= board.min_sharpness),
        Check("Edge margin", f"{margin:.0f} / min 8 px", found and margin >= 8),
        Check("Camera clock", "synced" if not timing_error(frame) else "waiting for valid timestamps", not timing_error(frame)),
        Check("Frame age", f"{(now - frame['received_monotonic_s']) * 1000:.0f} ms / max 250", capture_ready(frame, now)),
    ]
    for side, name in (("left", "Left"), ("right", "Right")):
        arm = state[side]
        age = now - arm["received_monotonic_s"]
        checks.append(Check(name + " feedback", f"{age * 1000:.0f} ms / max {limits.feedback_age_s * 1000:g}", 0 <= age <= limits.feedback_age_s))
        legal = True
        try:
            kin.validate(arm["q"])
        except ValueError:
            legal = False
        checks.append(Check(name + " joint limits", "in range" if legal else "out of range", legal))
        speed = float(np.max(np.abs(arm["velocity"])))
        checks.append(Check(name + " speed", f"{speed:.3f} / max {limits.still_velocity_rad_s:g} rad/s", speed <= limits.still_velocity_rad_s))
    elapsed = 0 if gate.since is None else max(0, now - gate.since)
    checks.extend([
        Check("Stable dwell", f"{elapsed:.1f} / min {limits.stable_s:g} s", stable),
        Check("Post-settle exposure", "ready" if gate.since is not None and frame.get("source_monotonic_s", 0) > gate.since + .1 else "waiting for settled frame", gate.since is not None and frame.get("source_monotonic_s", 0) > gate.since + .1),
    ])
    return checks


def route_checks(route, state, limits):
    arm = route["active_arm"]
    other = "right" if arm == "left" else "left"
    points = route["waypoints"]
    unique = not any(
        np.max(np.abs(np.asarray(state[arm]["q"]) - p["q"][arm])) < .025
        for p in points if p["kind"] == "capture"
    )
    parked = not points or np.max(np.abs(np.asarray(state[other]["q"]) - points[0]["q"][other])) <= limits.arrival_rad
    segment = True
    if points:
        try:
            Segment(points[-1]["q"][arm], state[arm]["q"], limits)
        except ValueError:
            segment = False
    return [
        Check("New pose", "different from saved poses" if unique else "too close to a saved pose", unique),
        Check("Parked arm", "unchanged" if parked else "return to first saved position", parked),
        Check("Segment duration", "in range" if segment else "reduce gap from last pose", segment),
    ]


def render(image, board, detection, title, checks, footer):
    view = overlay(image, board, detection, [], False)
    scale = min(1.0, 960 / view.shape[1], 760 / view.shape[0])
    view = cv2.resize(view, (round(view.shape[1] * scale), round(view.shape[0] * scale)))
    height = max(view.shape[0], 95 + 29 * len(checks) + 29 * len(footer))
    canvas = np.full((height, view.shape[1] + 670, 3), (32, 26, 22), np.uint8)
    canvas[:view.shape[0], :view.shape[1]] = view
    x = view.shape[1] + 14
    cv2.putText(canvas, title, (x, 30), cv2.FONT_HERSHEY_SIMPLEX, .65, (240,240,240), 1, cv2.LINE_AA)
    for index, check in enumerate(checks):
        color = (120,220,70) if check.passed else (65,175,255)
        text = f"{'PASS' if check.passed else 'WAIT'}  {check.label}: {check.value}"
        cv2.putText(canvas, text, (x, 63 + index * 29), cv2.FONT_HERSHEY_SIMPLEX, .53, color, 1, cv2.LINE_AA)
    for index, line in enumerate(footer):
        cv2.putText(canvas, line, (x, 85 + len(checks) * 29 + index * 29), cv2.FONT_HERSHEY_SIMPLEX, .49, (235,235,235), 1, cv2.LINE_AA)
    return canvas


def startup_image():
    image = np.full((680,1100,3), (32,26,22), np.uint8)
    cv2.putText(image, "Starting camera...", (30,70), cv2.FONT_HERSHEY_SIMPLEX, 1, (235,235,235), 2)
    return image
