from __future__ import annotations

from typing import Any


OPENPI_COMMIT = "15a9616a00943ada6c20a0f158e3adb39df2ccac"
LEROBOT_COMMIT = "0cf864870cf29f4738d3ade893e6fd13fbd7cdb5"
DATASET_FPS = 50
ACTION_HORIZON = 50
MODEL_ACTION_DIM = 32
IMAGE_SIZE = (640, 360)
CAMERA_KEYS = ("cam_high", "cam_left_wrist", "cam_right_wrist")
MOTOR_NAMES = (
    "left_j1",
    "left_j2",
    "left_j3",
    "left_j4",
    "left_j5",
    "left_j6",
    "left_gripper",
    "right_j1",
    "right_j2",
    "right_j3",
    "right_j4",
    "right_j5",
    "right_j6",
    "right_gripper",
)


def lerobot_features(mode: str) -> dict[str, dict[str, Any]]:
    features: dict[str, dict[str, Any]] = {
        "observation.state": {
            "dtype": "float32",
            "shape": (len(MOTOR_NAMES),),
            "names": [list(MOTOR_NAMES)],
        },
        "action": {
            "dtype": "float32",
            "shape": (len(MOTOR_NAMES),),
            "names": [list(MOTOR_NAMES)],
        },
    }
    width, height = IMAGE_SIZE
    for camera in CAMERA_KEYS:
        features[f"observation.images.{camera}"] = {
            "dtype": mode,
            "shape": (3, height, width),
            "names": ["channels", "height", "width"],
        }
    return features
