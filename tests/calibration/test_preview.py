from types import SimpleNamespace

import cv2
import numpy as np

from arx5_collection.calibration.board import Board, Detection
from arx5_collection.calibration.motion import MotionLimits
from arx5_collection.calibration.preview import Check, live_checks, render, route_checks


def test_each_check_is_independent_when_small_board_blocks_save(kin):
    board = Board(8, 11, 5)
    image = np.zeros((720, 1280, 3), np.uint8)
    detection = Detection(np.array([[100,100], [200,100], [200,200], [100,200]], np.float32), .011, 1200, "board too small")
    frame = {"image": image, "received_monotonic_s": 10., "received_wall_s": 10., "source_monotonic_s": 9.99, "source_time_s": 9.99, "clock": "global_time"}
    state = {s: {"q": [0, .948, .858, -.573, 0, 0], "velocity": [0]*6, "received_monotonic_s": 10.} for s in ("left", "right")}
    checks = live_checks(frame, detection, board, state, kin, MotionLimits(), SimpleNamespace(since=9.), True, 10.01)
    failed = [c.label for c in checks if not c.passed]
    assert failed == ["Coverage"]
    state["left"]["received_monotonic_s"] = 9.5
    checks = live_checks(frame, detection, board, state, kin, MotionLimits(), SimpleNamespace(since=9.), True, 10.01)
    assert [c.label for c in checks if not c.passed] == ["Coverage", "Left feedback"]
    assert next(c for c in checks if c.label == "Right feedback").passed


def test_panel_has_independent_green_and_orange_rows_without_changing_source():
    image = np.full((720,1280,3), 140, np.uint8)
    original = image.copy()
    view = render(image, Board(8,11,5), Detection(None,0,0,""), "Left wrist | WAIT", [Check("Coverage", "1.1% / min 1.5%", False), Check("Sharpness", "1200 / min 40", True)], ["SPACE save | BACKSPACE delete last"])
    assert (view[:,:,0] == 65).any()  # Orange WAIT text.
    assert np.any(np.all(view == (120,220,70), axis=2))  # Green PASS text.
    np.testing.assert_array_equal(image, original)
    assert view.shape[1] > image.shape[1]


def test_duplicate_and_parked_arm_checks_precede_keypress(route):
    state = {s: {"q": route["waypoints"][0]["q"][s]} for s in ("left", "right")}
    checks = route_checks(route, state, MotionLimits())
    assert not checks[0].passed
    assert checks[1].passed
    state["right"]["q"] = np.array(state["right"]["q"]) + .1
    assert not route_checks(route, state, MotionLimits())[1].passed
