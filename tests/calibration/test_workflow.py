from copy import deepcopy
from time import monotonic
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from arx5_collection.calibration import capture, workflow
from arx5_collection.calibration.board import Board
from arx5_collection.calibration.storage import read_json


@pytest.mark.parametrize("undo", [False, True])
@pytest.mark.parametrize("clock_error", ["", "camera clock settling"])
def test_space_saves_live_joints_and_duplicate_is_rejected(
    tmp_path, route, session, monkeypatch, clock_error, undo
):
    route = deepcopy(route)
    q = route["waypoints"][0]["q"]
    route["waypoints"] = []
    route["draft"] = True
    board = Board(**session["board"])
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

    keys = iter(
        [32, 8, 32, workflow.WINDOW_CLOSED]
        if undo
        else [ord("v"), ord("r"), 13, 27, 32, 32, workflow.WINDOW_CLOSED]
    )
    monkeypatch.setattr(workflow, "StableWindow", Stable)
    monkeypatch.setattr(workflow, "_window", lambda *a: None)
    monkeypatch.setattr(workflow, "_key", lambda *a: next(keys))
    monkeypatch.setattr(cv2, "destroyWindow", lambda *a: None)
    path = tmp_path / "routes/left-wrist.json"
    workflow.teach(hardware, route, path, session=session)
    saved = read_json(path)
    assert calls == ["gravity_compensation"]
    if clock_error:
        assert saved["waypoints"] == []
        assert not list((path.parent / "teaching").rglob("*.png"))
        return
    assert len(saved["waypoints"]) == 1 and saved["draft"]
    assert saved["waypoints"][0]["q"] == q
    point = saved["waypoints"][0]
    assert set(point) == {"pose_id", "q", "kind"}
    evidence = read_json(
        path.parent / "teaching" / route["route_id"] / (point["pose_id"] + ".json")
    )
    assert evidence["actual"]["left"]["q"] == q["left"]
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
    tmp_path, route, session, monkeypatch
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

        def move(self, q, **kwargs):
            events.append("move")
            return monotonic() + 2

        def require_ok(self):
            pass

        def target(self):
            return q

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(capture, "ReplayControl", Controller)
    monkeypatch.setattr(workflow, "_window", lambda *a: None)
    monkeypatch.setattr(workflow, "_key", lambda *a: 27)
    monkeypatch.setattr(cv2, "destroyWindow", lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        workflow.record(hardware, route, tmp_path / "run", session=session)
    assert events == ["start", "move", "stop"]
    saved = read_json(tmp_path / "run/run.json")
    assert saved["status"] == "partial" and saved["observations"] == []
    assert "operator stopped" in saved["error"]


def test_slow_detection_reads_feedback_after_detection(
    tmp_path, route, session, monkeypatch
):
    """120 ms detector must not make continuously updated feedback look stale."""
    from arx5_collection.calibration.board import Detection

    route = deepcopy(route)
    q = route["waypoints"][0]["q"]
    route.update(waypoints=[], draft=True)
    clock = [10.0]
    reads, panels = [], []
    image = np.full((720, 1280, 3), 120, np.uint8)
    corners = np.array(
        [[100 + x * 30, 100 + y * 30] for y in range(5) for x in range(7)], np.float32
    )

    def state():
        reads.append(clock[0])
        return {
            s: {
                "q": q[s],
                "velocity": [0.0] * 6,
                "gripper": 0.0,
                "received_monotonic_s": clock[0],
            }
            for s in q
        }

    def frame():
        now = clock[0]
        return {
            "image": image,
            "number": now,
            "source_monotonic_s": now - 0.01,
            "source_time_s": now - 0.01,
            "received_wall_s": now,
            "received_monotonic_s": now,
            "clock": "global_time",
        }

    def slow_detect(*a, **kw):
        clock[0] += 0.12
        return Detection(corners, 0.1, 800.0, "")

    def panel(image, board, detection, title, checks, footer):
        assert footer == ["SPACE save | BACKSPACE delete last"]
        panels.append(checks)
        return image

    def key(*a):
        assert len(panels) < 15
        if route["waypoints"]:
            return workflow.WINDOW_CLOSED
        return 32 if all(c.passed for c in panels[-1]) else -1

    hardware = SimpleNamespace(
        arms=SimpleNamespace(read=state, call=lambda _: None),
        camera=SimpleNamespace(latest=frame),
        supervisor=SimpleNamespace(require_running=lambda: None),
    )
    monkeypatch.setattr(workflow, "monotonic", lambda: clock[0])
    monkeypatch.setattr(workflow, "sleep", lambda _: None)
    monkeypatch.setattr(workflow, "detect", slow_detect)
    monkeypatch.setattr(workflow, "render", panel)
    monkeypatch.setattr(workflow, "_key", key)
    monkeypatch.setattr(workflow, "_window", lambda _: None)
    monkeypatch.setattr(cv2, "destroyWindow", lambda _: None)
    result = workflow.teach(hardware, route, tmp_path / "route.json", session=session)
    assert len(result["waypoints"]) == 1
    assert reads[0] == pytest.approx(10.12)
    assert all(
        next(c for c in checks if c.label == "Left feedback").passed
        for checks in panels
    )


@pytest.mark.parametrize("count,expected_draft", [(5, True), (25, False)])
def test_close_window_finalizes_or_preserves_draft(
    tmp_path, route, session, monkeypatch, count, expected_draft
):
    route = deepcopy(route)
    route.update(waypoints=route["waypoints"][:count], draft=True)
    q = route["waypoints"][-1]["q"]
    hardware = SimpleNamespace(
        arms=SimpleNamespace(
            read=lambda: {
                s: {
                    "q": q[s],
                    "velocity": [0] * 6,
                    "gripper": 0.0,
                    "received_monotonic_s": monotonic(),
                }
                for s in q
            },
            call=lambda _: None,
        ),
        camera=SimpleNamespace(
            latest=lambda: {
                "number": 1,
                "image": np.zeros((480, 848, 3), np.uint8),
                "received_monotonic_s": monotonic(),
            }
        ),
        supervisor=SimpleNamespace(require_running=lambda: None),
    )
    monkeypatch.setattr(workflow, "_key", lambda *a: workflow.WINDOW_CLOSED)
    monkeypatch.setattr(workflow, "_window", lambda _: None)
    monkeypatch.setattr(cv2, "destroyWindow", lambda _: None)
    path = tmp_path / "route.json"
    workflow.teach(hardware, route, path, session=session)
    assert read_json(path)["draft"] is expected_draft
