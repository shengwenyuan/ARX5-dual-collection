"""Bounded motion loop; no OpenCV, display, PNG encoding or file I/O in this thread."""

from __future__ import annotations

from collections import deque
from threading import Event, Lock, Thread
from time import monotonic, sleep

import numpy as np

from arx5_collection.ros2_adapters.reset import DEFAULT_HOME

from .geometry import Kinematics
from .motion import (
    MotionLimits,
    Segment,
    StaleArmFeedback,
    stationary,
    validate_sample,
    vector,
)


class ReplayControl:
    def __init__(
        self, arms, active_arm, limits: MotionLimits, clock=monotonic, sleep_fn=sleep
    ):
        self.arms, self.active_arm, self.limits = arms, active_arm, limits
        self.clock, self.sleep = clock, sleep_fn
        self.lock, self.stop_event = Lock(), Event()
        self.error = None
        self.segment = None
        self.segment_side = active_arm
        self.thread = None
        self.trace = deque(maxlen=100000)
        state = arms.read()
        self.targets = {s: vector(state[s]["q"]).copy() for s in ("left", "right")}
        self.goal = {s: q.copy() for s, q in self.targets.items()}
        self.segment_started = self.clock()
        self.expected_end = self.clock()

    def start(self):
        self.arms.call("enable_policy_control")
        self.thread = Thread(target=self._run, name="calibration-motion", daemon=True)
        self.thread.start()

    def move(self, q, *, side=None):
        side = self.active_arm if side is None else side
        if side not in ("left", "right"):
            raise ValueError("unknown arm")
        self.require_ok()
        with self.lock:
            if self.clock() < self.expected_end:
                raise RuntimeError("previous segment is still running")
            self.segment = Segment(self.targets[side], q, self.limits)
            self.segment_side = side
            self.segment_started = self.clock()
            self.expected_end = self.segment_started + self.segment.duration
            self.goal[side] = vector(q).copy()
        return self.expected_end

    def require_ok(self):
        if self.error:
            raise RuntimeError("calibration motion stopped") from self.error

    def target(self):
        with self.lock:
            return {s: q.copy() for s, q in self.goal.items()}

    def _run(self):
        previous_tick = self.clock()
        period = 1 / self.limits.rate_hz
        try:
            while not self.stop_event.is_set():
                now = self.clock()
                if now - previous_tick > 0.10:
                    raise RuntimeError("control loop overrun; no catch-up jump allowed")
                previous_tick = now
                state = self.arms.read()
                validate_sample(state, self.clock(), self.limits)
                with self.lock:
                    if self.segment:
                        self.targets[self.segment_side] = self.segment.at(
                            now - self.segment_started
                        )
                    command = {s: q.copy() for s, q in self.targets.items()}
                for side in ("left", "right"):
                    if (
                        np.max(np.abs(vector(state[side]["q"]) - command[side]))
                        > self.limits.following_error_rad
                    ):
                        raise RuntimeError(f"{side} tracking error")
                    if np.max(np.abs(vector(state[side]["velocity"]))) > (
                        self.limits.velocity_rad_s * 1.2 + 0.01
                    ):
                        raise RuntimeError(
                            f"{side} measured velocity exceeds calibration bound"
                        )
                self.arms.publish(command)
                self.trace.append(
                    {
                        "monotonic_s": now,
                        "target_q": {s: q.tolist() for s, q in command.items()},
                        "actual": state,
                    }
                )
                self.stop_event.wait(max(0.0, period - (self.clock() - now)))
        except BaseException as error:
            self.error = error
            try:
                self.arms.call("calibration_hold")
            except BaseException:
                pass  # Independent controller watchdog also latches after 250 ms.

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(4)
            if self.thread.is_alive():
                raise RuntimeError(
                    "motion thread failed to stop; controller watchdog will hold"
                )
        self.arms.call("calibration_hold")


class StableWindow:
    def __init__(self, limits):
        self.limits = limits
        self.since = None
        self.reference = None
        self.wait_reason = "release both arms"

    def update(self, sample, now, reference=None):
        validate_sample(sample, now, self.limits)
        if self.reference is None:
            self.reference = {s: sample[s]["q"] for s in ("left", "right")}
        expected = reference if reference is not None else self.reference
        if not stationary(sample, expected, self.limits):
            moving = [
                side
                for side in ("left", "right")
                if np.max(np.abs(vector(sample[side]["velocity"])))
                > self.limits.still_velocity_rad_s
            ]
            self.wait_reason = (
                "/".join(moving) + " speed" if moving else "position changed"
            )
            self.since = None
            self.reference = {s: sample[s]["q"] for s in ("left", "right")}
            return False
        if self.since is None:
            self.since = now
        self.wait_reason = "keep still"
        return now - self.since >= self.limits.stable_s


class TeachFeedback:
    """Only teach tolerates brief gaps; any gap restarts the stable window."""

    def __init__(self, arms, limits, gate, clock=monotonic):
        self.arms, self.limits, self.gate, self.clock = arms, limits, gate, clock
        self.unavailable_since = None

    def read(self):
        try:
            state = self.arms.read()
            stable = self.gate.update(state, self.clock())
        except StaleArmFeedback as error:
            now = self.clock()
            self.gate.since = self.gate.reference = None
            self.gate.wait_reason = "feedback unavailable"
            if self.unavailable_since is None:
                self.unavailable_since = now
            if now - self.unavailable_since >= 2.0:
                raise RuntimeError(
                    "示教关节反馈持续超时超过 2 秒，采集已中断"
                ) from error
            return error.sample, False
        self.unavailable_since = None
        return state, stable


def startup_steps(route):
    """Home both arms, then position the parked arm before the camera-carrying arm."""
    active = route["active_arm"]
    parked = "right" if active == "left" else "left"
    first = route["waypoints"][0]["q"]
    return [
        ("HOME", "left", DEFAULT_HOME),
        ("HOME", "right", DEFAULT_HOME),
        ("START", parked, first[parked]),
        ("START", active, first[active]),
    ]


def preflight_start(route, actual, limits):
    """Validate the full approach before enabling motion; no manual first-pose match."""
    validate_sample(actual, monotonic(), limits)
    kin = Kinematics.from_payload(route["kinematics"])
    positions = {s: actual[s]["q"] for s in ("left", "right")}
    for q in positions.values():
        kin.validate(q)
    if not stationary(actual, positions, limits):
        raise ValueError("release both arms and let them stop before automatic HOME")
    for _, side, target in startup_steps(route):
        kin.validate(target)
        Segment(positions[side], target, limits)
        positions[side] = target
