from pathlib import Path
from unittest.mock import patch
import runpy
import numpy as np
import pytest

from arx5_collection.calibration.board import Board, detect, overlay
from arx5_collection.production.cli import build_parser, main
from arx5_collection.production.lease import HardwareLease


def chessboard(board, cell=45):
    image = np.full((480, 848, 3), 190, np.uint8)
    for y in range(board.rows + 1):
        for x in range(board.columns + 1):
            image[
                100 + y * cell : 100 + (y + 1) * cell,
                150 + x * cell : 150 + (x + 1) * cell,
            ] = 255 if (x + y) % 2 else 0
    return image


def test_live_overlay_and_orientation_match_saved_physical_corner():
    board = Board(7, 5, 10)
    image = chessboard(board)
    detection = detect(image, board)
    assert detection.valid, detection.reason
    flipped = detect(image, board, reference=detection.corners[::-1])
    np.testing.assert_allclose(flipped.corners, detection.corners[::-1])
    preview = overlay(image, board, detection, ["READY", "SPACE save"], True)
    assert preview.shape == image.shape and not np.array_equal(image, preview)
    assert not detect(np.full_like(image, 128), board).valid


def test_four_role_flags_are_exclusive_and_stages_remain_simple():
    parser = build_parser()
    for role in ("left-wrist", "right-wrist", "overview-left", "overview-right"):
        args = parser.parse_args(["cali", "--" + role, "--teach"])
        assert args.role == role and args.stage == "teach"
    with pytest.raises(SystemExit):
        parser.parse_args(["cali", "--left-wrist", "--right-wrist"])
    with pytest.raises(SystemExit):
        parser.parse_args(["cali", "--left-wrist", "--teach", "--record"])


def test_offline_bad_json_never_opens_hardware(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{}")
    with patch("arx5_collection.calibration.hardware.Hardware") as hardware:
        assert main(["cali", "--left-wrist", "--check", "--poses", str(path)]) == 2
    hardware.assert_not_called()


def test_host_wrapper_forwards_exact_camera_and_stage():
    root = Path(__file__).resolve().parents[2]
    entry = runpy.run_path(root / "scripts/arx5")
    with patch("subprocess.call", return_value=0) as call:
        assert entry["cali"](["--overview-right", "--teach"]) == 0
    assert call.call_args.args[0][-4:] == [
        "arx5-collect",
        "cali",
        "--overview-right",
        "--teach",
    ]


def test_session_lock_excludes_second_owner_and_releases(tmp_path):
    a, b = (
        HardwareLease(tmp_path / "hardware.lock"),
        HardwareLease(tmp_path / "hardware.lock"),
    )
    a.acquire()
    with pytest.raises(RuntimeError, match="in use"):
        b.acquire()
    a.release()
    b.acquire()
    b.release()
    assert (tmp_path / "hardware.lock").exists()
