from __future__ import annotations

import unittest

from arx5_collection.metrics import (
    finite_scalar,
    finite_vector,
    split_arm_feedback,
    timing_summary,
)


class MetricsTest(unittest.TestCase):
    def test_finite_vector(self) -> None:
        self.assertEqual(finite_vector([1, 2], 2, "value"), [1.0, 2.0])
        with self.assertRaises(RuntimeError):
            finite_vector([1], 2, "value")

    def test_finite_scalar(self) -> None:
        self.assertEqual(finite_scalar("1.5", "value"), 1.5)

    def test_split_arm_feedback(self) -> None:
        joints, gripper = split_arm_feedback(range(7), "feedback")
        self.assertEqual(joints, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(gripper, 6.0)

    def test_timing_summary(self) -> None:
        summary = timing_summary([0, 20_000_000, 40_000_000])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["observed_hz"], 50.0)
        self.assertEqual(summary["max_gap_ms"], 20.0)


if __name__ == "__main__":
    unittest.main()
