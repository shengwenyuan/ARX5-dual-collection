from __future__ import annotations

import unittest

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.actions import make_state


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


if __name__ == "__main__":
    unittest.main()
