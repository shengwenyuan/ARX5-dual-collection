"""Portable paths carry targets; each replay independently earns its observations."""

from copy import deepcopy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from arx5_collection.calibration import capture
from arx5_collection.calibration.board import Board, Detection
from arx5_collection.calibration.geometry import inverse, rigid, handeye
from arx5_collection.calibration.orientation import align_orientations, corner_orders
from arx5_collection.calibration.routes import (
    portable_route,
    split_observations,
    validate,
)
from arx5_collection.calibration.storage import read_json


def test_legacy_route_loses_station_and_image_claims_without_changing_targets(
    route, session
):
    legacy = deepcopy(route)
    legacy.update(schema_version=1, **session, board_mount_id="w5-only")
    for p in legacy["waypoints"]:
        p.update(split="training", teaching={"corners": [[0, 0]], "image": "w5.png"})
    portable = portable_route(legacy)
    assert portable == route
    assert validate(portable, replay=True)["captures"] == 25
    assert (
        legacy["station"] == session["station"]
    )  # Conversion never edits original evidence.
    contaminated = {**portable, "station": session["station"]}
    with pytest.raises(ValueError, match="schema"):
        validate(contaminated)


@pytest.mark.parametrize(
    "count,training,heldout", [(0, 0, 0), (19, 14, 5), (20, 15, 5), (35, 28, 7)]
)
def test_split_is_determined_after_visual_skips(count, training, heldout):
    observations = [{"pose_id": str(i)} for i in range(count)]
    assert split_observations(observations) == {
        "accepted": count,
        "training": training,
        "validation": heldout,
    }
    assert sum(o["split"] == "validation" for o in observations) == heldout
    assert split_observations(deepcopy(observations)) == split_observations(
        observations
    )


@pytest.mark.parametrize("fail_at", [None, 6])
def test_replay_visits_all_candidates_but_hardware_fault_aborts(
    tmp_path, route, session, monkeypatch, fail_at
):
    from time import monotonic

    q = route["waypoints"][0]["q"]
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
            }
        ),
        camera=SimpleNamespace(metadata={"serial": "cl", "profile": route["profile"]}),
    )
    visits, events = [], []

    class Controller:
        trace = []

        def __init__(self, *a):
            pass

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

        def require_ok(self):
            pass

    def pose(hardware, control, route, board, point, index, window, show):
        visits.append(point["pose_id"])
        if index == fail_at:
            raise RuntimeError("arm feedback failed")
        if index < 5:
            return None, "skipped", "board not fully detected"
        return (
            (np.zeros((480, 848, 3), np.uint8), {"pose_id": point["pose_id"]}),
            "captured",
            "",
        )

    monkeypatch.setattr(capture, "ReplayControl", Controller)
    monkeypatch.setattr(capture, "capture_pose", pose)
    monkeypatch.setattr(capture, "prepare_start", lambda *a: events.append("prepared"))
    monkeypatch.setattr(cv2, "destroyWindow", lambda _: None)
    output = tmp_path / "run"

    def record():
        return capture.record_route(
            hardware,
            route,
            output,
            session=session,
            prepared_window="test",
            show=None,
            open_window=None,
        )

    if fail_at is None:
        result = record()
        assert len(visits) == 25
        assert result["status"] == "completed"
        assert result["selection"] == {
            "accepted": 20,
            "training": 15,
            "validation": 5,
            "skipped": 5,
            "attempted": 25,
        }
        assert read_json(output / "route.json") == route
        assert result["station"] == session["station"]
    else:
        with pytest.raises(RuntimeError, match="feedback"):
            record()
        assert len(visits) == fail_at + 1
        result = read_json(output / "run.json")
        assert result["status"] == "partial"
        assert result["error"] == "arm feedback failed"
    assert events == ["start", "prepared", "stop"]


def test_absent_board_skips_after_capture_window_without_station_pixel_reference(
    route, session, monkeypatch
):
    ticks = [10.0]
    q = route["waypoints"][0]["q"]

    def read():
        return {
            s: {
                "q": q[s],
                "velocity": [0] * 6,
                "gripper": 0.0,
                "received_monotonic_s": ticks[0],
            }
            for s in q
        }

    def frame():
        return {
            "image": np.zeros((480, 848, 3), np.uint8),
            "number": ticks[0],
            "source_monotonic_s": ticks[0] - 0.01,
            "received_monotonic_s": ticks[0],
            "clock": "global_time",
        }

    def detect(image, board):  # No reference argument allowed.
        return Detection(None, 0.0, 0.0, "board not fully detected")

    hardware = SimpleNamespace(
        arms=SimpleNamespace(read=read),
        camera=SimpleNamespace(latest=frame),
        supervisor=SimpleNamespace(require_running=lambda: None),
    )
    control = SimpleNamespace(
        require_ok=lambda: None, move=lambda q: 10.0, target=lambda: q
    )
    monkeypatch.setattr(capture, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        capture, "sleep", lambda _: ticks.__setitem__(0, ticks[0] + 0.1)
    )
    monkeypatch.setattr(capture, "detect", detect)
    monkeypatch.setattr(capture, "render", lambda *a: a[0])
    result = capture.capture_pose(
        hardware,
        control,
        route,
        Board(**session["board"]),
        route["waypoints"][0],
        0,
        "test",
        lambda *a: -1,
    )
    assert result == (None, "skipped", "board not fully detected")
    assert 12.6 <= ticks[0] < 13.0


@pytest.mark.parametrize("mode", ["eye_in_hand", "eye_to_hand"])
@pytest.mark.parametrize("shape", [(7, 5), (5, 5)])
def test_robot_rotations_resolve_arbitrary_chessboard_symmetry(kin, mode, shape):
    board = Board(*shape, 10)
    k = np.array([[650.0, 0, 424.0], [0, 660.0, 240.0], [0, 0, 1.0]])
    intrinsic = {"K": k.tolist(), "distortion": [0.0] * 5}
    home = np.array([0, 0.948, 0.858, -0.573, 0, 0])
    x = rigid([0.12, -0.15, 0.08], [0.03, 0.02, 0.01])
    z0 = rigid([0.4, -0.3, 0.1], [-0.03, -0.02, 0.4])
    a0 = kin.fk(home)
    if mode == "eye_to_hand":
        a0 = inverse(a0)
    y = a0 @ x @ z0
    rng = np.random.default_rng(827)
    observations = []
    for i in range(25):
        a = kin.fk(home + rng.uniform(-0.15, 0.15, 6))
        operator = a if mode == "eye_in_hand" else inverse(a)
        z = inverse(x) @ inverse(operator) @ y
        corners = cv2.projectPoints(
            board.points(), cv2.Rodrigues(z[:3, :3])[0], z[:3, 3], k, None
        )[0].reshape(-1, 2)
        options = corner_orders(corners, board)
        observations.append(
            {
                "pose_id": str(i),
                "T_base_link6": a.tolist(),
                "detection": {
                    "corners": options[int(rng.integers(len(options)))].tolist()
                },
            }
        )
    split_observations(observations)
    aligned, report = align_orientations({"mode": mode}, observations, board, intrinsic)
    training = [i for i, o in enumerate(observations) if o["split"] == "training"]
    fitted, _, _ = handeye(
        mode,
        [np.array(observations[i]["T_base_link6"]) for i in training],
        [aligned[i][0] for i in training],
    )
    np.testing.assert_allclose(fitted, x, atol=2e-5)
    # Reordering held-out corners cannot change the training orientation fit.
    changed = deepcopy(observations)
    for o in changed:
        if o["split"] == "validation":
            o["detection"]["corners"].reverse()
    _, second = align_orientations({"mode": mode}, changed, board, intrinsic)
    for i in training:
        assert (
            report["selected_symmetry"][str(i)] == second["selected_symmetry"][str(i)]
        )
