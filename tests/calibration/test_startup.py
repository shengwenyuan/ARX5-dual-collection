"""Automatic HOME approach must settle before any route observation is attempted."""

from types import SimpleNamespace

import numpy as np
import pytest

from arx5_collection.calibration import capture
from arx5_collection.calibration.motion import MotionLimits
from arx5_collection.calibration.replay import ReplayControl
from arx5_collection.ros2_adapters.reset import DEFAULT_HOME


def simulated_start(route, monkeypatch, *, stalled=False, key=-1):
    clock = [10.0]
    q = {s: np.zeros(6) for s in ("left", "right")}
    goal = {s: v.copy() for s, v in q.items()}
    calls, arrivals = [], []

    def state():
        return {
            s: {
                "q": v.tolist(),
                "velocity": [0.0] * 6,
                "gripper": -0.8,
                "received_monotonic_s": clock[0],
            }
            for s, v in q.items()
        }

    def move(target, *, side):
        calls.append((side, list(target)))
        goal[side] = np.array(target)
        if not stalled:
            q[side] = np.array(target)
        return clock[0] + 0.5

    hardware = SimpleNamespace(
        arms=SimpleNamespace(read=state),
        camera=SimpleNamespace(
            latest=lambda: {"image": np.zeros((480, 848, 3), np.uint8)}
        ),
        supervisor=SimpleNamespace(require_running=lambda: None),
    )
    control = SimpleNamespace(move=move, target=lambda: goal, require_ok=lambda: None)
    monkeypatch.setattr(capture, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        capture, "sleep", lambda _: clock.__setitem__(0, clock[0] + 0.1)
    )
    monkeypatch.setattr(capture, "overlay", lambda image, *a: image)

    def start():
        capture.prepare_start(
            hardware, control, route, "test", lambda *a: key, arrivals.append
        )

    return start, calls, arrivals


@pytest.mark.parametrize("active", ["left", "right"])
def test_both_home_then_parked_arm_then_active_first_pose(route, monkeypatch, active):
    route["active_arm"] = active
    start, calls, arrivals = simulated_start(route, monkeypatch)
    start()
    parked = "right" if active == "left" else "left"
    first = route["waypoints"][0]["q"]
    assert calls == [
        ("left", list(DEFAULT_HOME)),
        ("right", list(DEFAULT_HOME)),
        (parked, first[parked]),
        (active, first[active]),
    ]
    assert [s["phase"] for s in arrivals] == ["HOME", "HOME", "START", "START"]
    for s in ("left", "right"):
        assert arrivals[-1]["actual"][s]["q"] == first[s]
        assert arrivals[-1]["actual"][s]["gripper"] == -0.8


def test_home_timeout_does_not_advance_to_other_arm_or_first_pose(route, monkeypatch):
    start, calls, arrivals = simulated_start(route, monkeypatch, stalled=True)
    with pytest.raises(TimeoutError, match="HOME left"):
        start()
    assert calls == [("left", list(DEFAULT_HOME))]
    assert arrivals == []


def test_home_escape_does_not_advance(route, monkeypatch):
    start, calls, arrivals = simulated_start(route, monkeypatch, key=27)
    with pytest.raises(KeyboardInterrupt):
        start()
    assert len(calls) == 1 and not arrivals


def test_motion_can_position_inactive_arm_without_moving_active_arm():
    state = {
        s: {
            "q": [0.0] * 6,
            "velocity": [0.0] * 6,
            "gripper": -0.8,
            "received_monotonic_s": 10.0,
        }
        for s in ("left", "right")
    }
    clock, published = [10.0], []

    def read():
        for arm in state.values():
            arm["received_monotonic_s"] = clock[0]
        return state

    def publish(command):
        published.append({s: q.copy() for s, q in command.items()})
        for side, q in command.items():
            state[side]["q"] = q.tolist()

    arms = SimpleNamespace(read=read, publish=publish, call=lambda _: None)
    control = ReplayControl(arms, "left", MotionLimits(), clock=lambda: clock[0])
    end = control.move([0.1] * 6, side="right")
    assert end > 10.0
    assert control.segment_side == "right"
    np.testing.assert_array_equal(control.target()["left"], [0.0] * 6)
    np.testing.assert_array_equal(control.target()["right"], [0.1] * 6)
    np.testing.assert_array_equal(control.segment.at(0.0), [0.0] * 6)
    np.testing.assert_array_equal(control.segment.at(end - 10.0), [0.1] * 6)
    with pytest.raises(RuntimeError, match="still running"):
        control.move([0.0] * 6, side="left")

    control.stop_event = SimpleNamespace(
        is_set=lambda: clock[0] > end + 0.02,
        wait=lambda delay: clock.__setitem__(0, clock[0] + delay),
    )
    control._run()
    assert control.error is None
    assert published
    for command in published:
        np.testing.assert_array_equal(command["left"], [0.0] * 6)
    np.testing.assert_allclose(published[-1]["right"], [0.1] * 6)
