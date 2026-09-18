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
def route(kin):
    identity = {
        "station_id": "test-only",
        "arms": {"left": "l", "right": "r"},
        "cameras": {"left": "cl", "right": "cr", "overview": "co"},
        "sdk_type": 2,
    }
    r = new_route("left-wrist", identity, Board(7, 5, 10), kin, "test-setup", PROFILE)
    rng = np.random.default_rng(45)
    home = np.array([0, 0.948, 0.858, -0.573, 0, 0])
    for i in range(25):
        q = home + rng.uniform(-0.22, 0.22, 6)
        r["waypoints"].append(
            {
                "pose_id": f"test-{i}",
                "q": {"left": q.tolist(), "right": home.tolist()},
                "kind": "capture",
                "split": "validation" if i % 5 == 4 else "training",
                "teaching": {
                    "origin_confirmed": True,
                    "corners": np.zeros((35, 2)).tolist(),
                },
            }
        )
    r["draft"] = False
    return r
