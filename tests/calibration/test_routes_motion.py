from copy import deepcopy
from time import monotonic
import numpy as np
import pytest

from arx5_collection.calibration.motion import MotionLimits, Segment
from arx5_collection.calibration.replay import (
    StableWindow,
    preflight_start,
    ReplayControl,
)
from arx5_collection.calibration.routes import validate, finalize
from arx5_collection.calibration.storage import read_json, write_json


def sample(q=None, now=None):
    now = monotonic() if now is None else now
    return {
        s: {
            "q": [0] * 6 if q is None else list(q),
            "velocity": [0] * 6,
            "gripper": -0.8,
            "received_monotonic_s": now,
        }
        for s in ("left", "right")
    }


def test_quintic_obeys_velocity_acceleration_and_end_conditions():
    limits = MotionLimits()
    for distance in (0.001, 0.05, 0.7, 2.0):
        segment = Segment([0] * 6, [distance, -distance, 0, 0, 0, 0], limits)
        times = np.linspace(0, segment.duration, 20001)
        q = np.array([segment.at(t) for t in times])
        velocity = np.gradient(q, times, axis=0)
        acceleration = np.gradient(velocity, times, axis=0)
        assert abs(velocity).max() <= limits.velocity_rad_s + 1e-7
        assert abs(acceleration).max() <= limits.acceleration_rad_s2 + 1e-7
        assert abs(velocity[[0, -1]]).max() < 1e-6
        np.testing.assert_allclose(q[0], [0] * 6)
        np.testing.assert_allclose(q[-1], segment.end)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"velocity_rad_s": 1.0},
        {"acceleration_rad_s2": 1.0},
        {"velocity_rad_s": float("nan")},
        {"stable_s": 0.1},
    ],
)
def test_motion_limits_cannot_silently_increase(kwargs):
    with pytest.raises(ValueError):
        MotionLimits(**kwargs)


def test_whole_route_validate_and_rejects_wrong_role_duplicate_or_moving_parked_arm(
    route,
):
    assert validate(route, "left-wrist", replay=True)["captures"] == 25
    with pytest.raises(ValueError, match="role"):
        validate(route, "right-wrist", replay=True)
    duplicate = deepcopy(route)
    duplicate["waypoints"][-1]["q"] = duplicate["waypoints"][0]["q"]
    with pytest.raises(ValueError, match="duplicate"):
        validate(duplicate, replay=True)
    changed = deepcopy(route)
    changed["waypoints"][-1]["q"]["right"][0] += 0.2
    with pytest.raises(ValueError, match="parked arm"):
        validate(changed, replay=True)


def test_draft_and_missing_validation_do_not_replay(route):
    route["draft"] = True
    with pytest.raises(ValueError, match="finish route"):
        validate(route, replay=True)
    assert not finalize(route)["draft"]
    route["waypoints"] = route["waypoints"][:5]
    with pytest.raises(ValueError):
        finalize(route)


def test_json_rejects_nan_duplicate_keys_and_preserves_previous_atomic_file(tmp_path):
    p = tmp_path / "route.json"
    write_json(p, {"a": 1})
    with pytest.raises(ValueError):
        write_json(p, {"a": float("nan")})
    assert read_json(p) == {"a": 1}
    p.write_text('{"a":1,"a":2}')
    with pytest.raises(ValueError, match="duplicate"):
        read_json(p)


def test_stability_requires_fresh_continuous_readings():
    limits = MotionLimits()
    gate = StableWindow(limits)
    assert not gate.update(sample(now=10), 10)
    assert gate.update(sample(now=10.7), 10.7)
    moving = sample(now=10.8)
    moving["left"]["velocity"][2] = 0.1
    assert not gate.update(moving, 10.8)
    assert not gate.update(sample(now=11), 11)
    with pytest.raises(RuntimeError, match="stale"):
        gate.update(sample(now=11), 12)


def test_start_mismatch_never_invokes_a_motion(route):
    with pytest.raises(ValueError, match="start mismatch"):
        preflight_start(route, sample(), MotionLimits())


def test_control_fault_holds_and_never_catches_up():
    class Arms:
        def __init__(self):
            self.calls = []

        def read(self):
            return sample(now=0)

        def publish(self, q):
            self.calls.append("publish")

        def call(self, service):
            self.calls.append(service)

    arms = Arms()
    ticks = iter([0, 0, 0, 0.2])
    control = ReplayControl(arms, "left", MotionLimits(), clock=lambda: next(ticks))
    control._run()
    assert isinstance(control.error, RuntimeError)
    assert arms.calls[-1] == "calibration_hold"
    assert "publish" not in arms.calls


def test_teach_feedback_gap_resets_stability_and_recovers():
    from types import SimpleNamespace
    from arx5_collection.calibration.motion import StaleArmFeedback, validate_sample
    from arx5_collection.calibration.replay import TeachFeedback
    limits = MotionLimits()
    clock = [10.]
    state = [sample(now=10)]
    gate = StableWindow(limits)
    def read():
        validate_sample(state[0], clock[0], limits)
        return state[0]
    feedback = TeachFeedback(SimpleNamespace(read=read), limits, gate, clock=lambda: clock[0])
    assert not feedback.read()[1]
    clock[0] = 10.7
    state[0] = sample(now=10.7)
    assert feedback.read()[1]
    clock[0] = 10.9  # No new feedback: transient wait, not a successful stable sample.
    assert not feedback.read()[1]
    assert gate.since is None
    clock[0] = 11.0
    state[0] = sample(now=11.)
    assert not feedback.read()[1]
    clock[0] = 11.7
    state[0] = sample(now=11.7)
    assert feedback.read()[1]
    clock[0] = 12.
    assert not feedback.read()[1]
    clock[0] = 14.1
    with pytest.raises(RuntimeError, match="持续超时"):
        feedback.read()
    # Replay's validator still fails immediately on exactly the same stale state.
    with pytest.raises(StaleArmFeedback):
        gate.update(state[0], clock[0])


def test_teach_never_swallows_controller_or_nonfinite_failure():
    from types import SimpleNamespace
    from arx5_collection.calibration.replay import TeachFeedback
    limits = MotionLimits()
    for error in (RuntimeError("arm feedback failed"), ValueError("nonfinite robot readback")):
        def read():
            raise error
        feedback = TeachFeedback(SimpleNamespace(read=read), limits, StableWindow(limits))
        with pytest.raises(type(error), match=str(error)):
            feedback.read()
