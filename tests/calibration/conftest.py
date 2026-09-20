from pathlib import Path
import numpy as np
import pytest

from arx5_collection.calibration.board import Board
from arx5_collection.calibration.geometry import Kinematics
from arx5_collection.calibration.hardware import PROFILE
from arx5_collection.calibration.routes import new_route

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def kin():
    return Kinematics.load(ROOT / "config/calibration/X5.urdf")


@pytest.fixture
def session():
    identity = {
        "station_id": "test-only",
        "arms": {"left": "l", "right": "r"},
        "cameras": {"left": "cl", "right": "cr", "overview": "co"},
        "sdk_type": 2,
    }
    return {
        "station": identity,
        "board": Board(7, 5, 10).payload(),
        "setup_id": "test-setup",
    }


@pytest.fixture
def route(kin):
    r = new_route("left-wrist", kin, PROFILE)
    rng = np.random.default_rng(45)
    home = np.array([0, 0.948, 0.858, -0.573, 0, 0])
    for i in range(25):
        q = home + rng.uniform(-0.22, 0.22, 6)
        r["waypoints"].append(
            {
                "pose_id": f"test-{i}",
                "q": {"left": q.tolist(), "right": home.tolist()},
                "kind": "capture",
            }
        )
    r["draft"] = False
    return r
