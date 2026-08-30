from __future__ import annotations

import unittest

from arx5_collection.pi05_dataset.lerobot_contract import CAMERA_KEYS
from arx5_collection.pi05_dataset.lerobot_contract import DATASET_FPS
from arx5_collection.pi05_dataset.lerobot_contract import MOTOR_NAMES
from arx5_collection.pi05_dataset.lerobot_contract import lerobot_features


class LeRobotContractTest(unittest.TestCase):
    def test_contract_defines_joint_and_camera_features_once(self) -> None:
        features = lerobot_features("video")

        self.assertEqual(DATASET_FPS, 50)
        self.assertEqual(len(MOTOR_NAMES), 14)
        self.assertEqual(features["action"]["shape"], (14,))
        self.assertEqual(
            {key.removeprefix("observation.images.") for key in features if "images" in key},
            set(CAMERA_KEYS),
        )

if __name__ == "__main__":
    unittest.main()
