from __future__ import annotations

import unittest

from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import MessageRef
from arx5_collection.common.gripper import ARX5_GRIPPER_CALIBRATION
from arx5_collection.common.gripper import GripperCalibration
from arx5_collection.dataset_pipeline.mining_stage.action_mining.utils import make_state


class ActionsTest(unittest.TestCase):
    def test_orders_arms_and_normalizes_grippers(self) -> None:
        left = ArmSample(MessageRef("/left", 0, 1, 1), (1, 2, 3, 4, 5, 6), 20)
        right = ArmSample(MessageRef("/right", 0, 1, 1), (7, 8, 9, 10, 11, 12), -5)

        state = make_state(
            left,
            right,
            GripperCalibration(open_value=10, closed_value=30),
            GripperCalibration(open_value=0, closed_value=-10),
        )

        self.assertEqual(state, (1, 2, 3, 4, 5, 6, 0.5, 7, 8, 9, 10, 11, 12, 0.5))

    def test_rejects_values_far_outside_calibration(self) -> None:
        calibration = GripperCalibration(open_value=0, closed_value=1)
        with self.assertRaises(ValueError):
            calibration.normalize(2)

    def test_arx5_contract_clamps_broad_asymmetric_boundary_overshoot(self) -> None:
        self.assertEqual(ARX5_GRIPPER_CALIBRATION.normalize(-3.5), 0.0)
        self.assertEqual(ARX5_GRIPPER_CALIBRATION.normalize(0.2), 1.0)
        with self.assertRaises(ValueError):
            ARX5_GRIPPER_CALIBRATION.normalize(-3.6)
        with self.assertRaises(ValueError):
            ARX5_GRIPPER_CALIBRATION.normalize(0.4)

    def test_arx5_contract_denormalizes_to_device_range(self) -> None:
        self.assertEqual(ARX5_GRIPPER_CALIBRATION.denormalize(0.0), -3.4)
        self.assertEqual(ARX5_GRIPPER_CALIBRATION.denormalize(1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
