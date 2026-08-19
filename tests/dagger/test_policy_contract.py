from __future__ import annotations

import math
import unittest

from arx5_collection.dagger.models import (
    PI05_V2_ACTION_DIMENSION,
    PI05_V2_ACTION_HORIZON,
    PI05_V2_CONTROL_RATE_HZ,
    PI05_V2_EXECUTION_STEPS,
    InferenceTicket,
)


def chunk(value: float = 0.0) -> tuple[tuple[float, ...], ...]:
    return ((value,) * PI05_V2_ACTION_DIMENSION,) * PI05_V2_ACTION_HORIZON


class Pi05V2PolicyContractTest(unittest.TestCase):
    def test_freezes_accepted_execution_contract(self) -> None:
        self.assertEqual(PI05_V2_ACTION_HORIZON, 50)
        self.assertEqual(PI05_V2_EXECUTION_STEPS, 10)
        self.assertEqual(PI05_V2_CONTROL_RATE_HZ, 30.0)

        ticket = InferenceTicket("inference", 2, "a" * 64, chunk())
        self.assertEqual(len(ticket.execution_chunk), 10)
        self.assertTrue(all(len(action) == 14 for action in ticket.execution_chunk))

    def test_rejects_wrong_horizon_or_dimension(self) -> None:
        with self.assertRaisesRegex(ValueError, "50 steps"):
            InferenceTicket("inference", 0, "a" * 64, chunk()[:-1])
        with self.assertRaisesRegex(ValueError, "14 values"):
            InferenceTicket("inference", 0, "a" * 64, ((0.0,) * 13,) * 50)

    def test_rejects_non_finite_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            InferenceTicket("inference", 0, "a" * 64, chunk(math.nan))


if __name__ == "__main__":
    unittest.main()
