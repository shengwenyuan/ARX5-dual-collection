from __future__ import annotations

import unittest

from arx5_collection.pi05_dataset.openpi_adapter import arx5_repack_structure
from arx5_collection.pi05_dataset.openpi_contract import CAMERA_KEYS
from arx5_collection.pi05_dataset.openpi_contract import DATASET_FPS
from arx5_collection.pi05_dataset.openpi_contract import MOTOR_NAMES
from arx5_collection.pi05_dataset.openpi_contract import lerobot_features


class OpenPiAdapterTest(unittest.TestCase):
    def test_contract_defines_joint_and_camera_features_once(self) -> None:
        features = lerobot_features("video")

        self.assertEqual(DATASET_FPS, 50)
        self.assertEqual(len(MOTOR_NAMES), 14)
        self.assertEqual(features["action"]["shape"], (14,))
        self.assertEqual(
            {key.removeprefix("observation.images.") for key in features if "images" in key},
            set(CAMERA_KEYS),
        )

    def test_repack_preserves_task_prompt_and_joint_fields(self) -> None:
        structure = arx5_repack_structure()

        self.assertEqual(structure["state"], "observation.state")
        self.assertEqual(structure["actions"], "action")
        self.assertEqual(structure["prompt"], "prompt")
        self.assertEqual(
            structure["images"],
            {
                "cam_high": "observation.images.cam_high",
                "cam_left_wrist": "observation.images.cam_left_wrist",
                "cam_right_wrist": "observation.images.cam_right_wrist",
            },
        )


if __name__ == "__main__":
    unittest.main()
