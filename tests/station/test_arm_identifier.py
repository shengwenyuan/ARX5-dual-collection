from __future__ import annotations

import unittest

from arx5_collection.station.arm_identifier import (
    ArmIdentificationError,
    MovementDetector,
)


ZERO = (0.0,) * 6


class MovementDetectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = MovementDetector(0.05)

    def test_identifies_only_one_moved_provisional_arm(self) -> None:
        scores = self.detector.scores(
            {"left": ZERO, "right": ZERO},
            {"left": (0.0, 0.06, 0.0, 0.0, 0.0, 0.0), "right": ZERO},
        )
        self.assertEqual(self.detector.classify(scores), "left")

    def test_rejects_both_arms_moving(self) -> None:
        with self.assertRaisesRegex(ArmIdentificationError, "both provisional"):
            self.detector.classify({"left": 0.08, "right": 0.06})

    def test_small_or_uncertain_changes_do_not_guess(self) -> None:
        self.assertIsNone(self.detector.classify({"left": 0.04, "right": 0.0}))
        self.assertIsNone(self.detector.classify({"left": 0.06, "right": 0.03}))


if __name__ == "__main__":
    unittest.main()
