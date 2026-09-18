from copy import deepcopy
from time import monotonic
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from arx5_collection.calibration import workflow
from arx5_collection.calibration.board import Board
from arx5_collection.calibration.storage import read_json


@pytest.mark.parametrize("clock_error", ["", "camera clock settling"])
def test_space_saves_live_joints_and_duplicate_is_rejected(
    tmp_path, route, monkeypatch, clock_error
):
    route = deepcopy(route)
    q = route["waypoints"][0]["q"]
    route["waypoints"] = []
    route["draft"] = True
    board = Board(**route["board"])
    image = np.full((480, 848, 3), 180, np.uint8)
    for y in range(board.rows + 1):
        for x in range(board.columns + 1):
            image[
                100 + y * 40 : 100 + (y + 1) * 40, 150 + x * 40 : 150 + (x + 1) * 40
            ] = 245 if (x + y) % 2 else 8

    def state():
        return {
            s: {
                "q": q[s],
                "velocity": [0] * 6,
                "gripper": -0.8,
                "received_monotonic_s": monotonic(),
            }
            for s in ("left", "right")
        }

    def frame():
        now = monotonic()
        return {
            "image": image,
            "number": 1,
            "source_monotonic_s": now - 0.01,
            "source_time_s": now - 0.01,
            "received_wall_s": now,
            "received_monotonic_s": now,
            "clock": "global_time",
            "clock_error": clock_error,
        }

    calls = []
    hardware = SimpleNamespace(
        arms=SimpleNamespace(read=state, call=calls.append),
        camera=SimpleNamespace(latest=frame),
        supervisor=SimpleNamespace(require_running=lambda: None),
    )

    class Stable:
        since = monotonic() - 1

        def __init__(self, *a):
            pass

        def update(self, *a):
            return True

    keys = iter([32, 32, 27])
    monkeypatch.setattr(workflow, "StableWindow", Stable)
    monkeypatch.setattr(workflow, "_window", lambda *a: None)
    monkeypatch.setattr(workflow, "_key", lambda *a: next(keys))
    monkeypatch.setattr(cv2, "destroyWindow", lambda *a: None)
    path = tmp_path / "routes/left-wrist.json"
    workflow.teach(hardware, route, path)
    saved = read_json(path)
    assert calls == ["gravity_compensation"]
    if clock_error:
        assert saved["waypoints"] == []
        assert not list((path.parent / "teaching").rglob("*.png"))
        return
    assert len(saved["waypoints"]) == 1 and saved["draft"]
    assert saved["waypoints"][0]["q"] == q
    assert saved["waypoints"][0]["teaching"]["actual"]["left"]["q"] == q["left"]
    assert list((path.parent / "teaching").rglob("*.png"))


def test_board_is_removed_before_gravity_and_hardware_exit():
    calls = []
    hardware = SimpleNamespace(
        arms=SimpleNamespace(call=lambda method: calls.append(method))
    )
    workflow.park(hardware, True, prompt=lambda message: calls.append(message))
    assert "取下" in calls[0]
    assert calls[1] == "gravity_compensation"
    assert "归位" in calls[2]


def test_escape_during_replay_stops_without_advancing_and_preserves_partial(
    tmp_path, route, monkeypatch
):
    import pytest

    from arx5_collection.calibration.hardware import PROFILE

    q = route["waypoints"][0]["q"]
    now = monotonic()
    actual = {
        s: {
            "q": q[s],
            "velocity": [0] * 6,
            "gripper": -0.8,
            "received_monotonic_s": now,
        }
        for s in ("left", "right")
    }
    image = np.full((480, 848, 3), 128, np.uint8)
    frame = {
        "image": image,
        "number": 1,
        "source_monotonic_s": now,
        "received_monotonic_s": now,
    }
    hardware = SimpleNamespace(
        arms=SimpleNamespace(read=lambda: actual),
        camera=SimpleNamespace(
            latest=lambda: frame, metadata={"serial": "cl", "profile": PROFILE}
        ),
        supervisor=SimpleNamespace(require_running=lambda: None),
    )
    events = []

    class Controller:
        trace = []

        def __init__(self, *a):
            pass

        def start(self):
            events.append("start")

        def move(self, q):
            events.append("move")
            return monotonic() + 2

        def require_ok(self):
            pass

        def target(self):
            return q

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(workflow, "ReplayControl", Controller)
    monkeypatch.setattr(workflow, "_window", lambda *a: None)
    monkeypatch.setattr(workflow, "_key", lambda *a: 27)
    monkeypatch.setattr(cv2, "destroyWindow", lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        workflow.record(hardware, route, tmp_path / "run")
    assert events == ["start", "move", "stop"]
    saved = read_json(tmp_path / "run/run.json")
    assert saved["status"] == "partial" and saved["observations"] == []
    assert "operator stopped" in saved["error"]
