from __future__ import annotations

import unittest

from arx5_collection.collection.runtime.checks import (
    CheckFailure,
    CheckPhase,
    CheckResult,
    run_checks,
)


class CheckProtocolTest(unittest.TestCase):
    def test_returns_all_passed_results(self) -> None:
        results = run_checks(
            [
                lambda: CheckResult("config", CheckPhase.SESSION, True, "valid"),
                lambda: CheckResult("disk", CheckPhase.EPISODE, True, "100 GiB"),
            ]
        )
        self.assertEqual([result.name for result in results], ["config", "disk"])

    def test_failure_preserves_phase_results(self) -> None:
        checks = [
            lambda: CheckResult("config", CheckPhase.SESSION, True, "valid"),
            lambda: CheckResult("camera", CheckPhase.ROS, False, "no telemetry"),
        ]
        with self.assertRaises(CheckFailure) as caught:
            run_checks(checks)
        self.assertEqual(caught.exception.results[1].phase, CheckPhase.ROS)
        self.assertIn("camera", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
