from __future__ import annotations

import math
import unittest

from arx5_collection.dagger.models import (
    InferenceTicket,
    PolicyExecutionProfile,
)


PROFILE = PolicyExecutionProfile(50, 14, 10, 25.0)


def chunk(
    value: float = 0.0,
    profile: PolicyExecutionProfile = PROFILE,
) -> tuple[tuple[float, ...], ...]:
    return ((value,) * profile.action_dimension,) * profile.action_chunk_size


class PolicyExecutionContractTest(unittest.TestCase):
    def test_current_experiment_uses_configured_execution_contract(self) -> None:
        ticket = InferenceTicket("inference", 2, "a" * 64, chunk(), PROFILE)
        self.assertEqual(len(ticket.execution_chunk), 10)
        self.assertTrue(all(len(action) == 14 for action in ticket.execution_chunk))
        self.assertEqual(ticket.execution.control_rate_hz, 25.0)

    def test_supports_a_different_model_execution_profile(self) -> None:
        profile = PolicyExecutionProfile(12, 8, 4, 20.0)
        ticket = InferenceTicket(
            "inference", 2, "a" * 64, chunk(profile=profile), profile
        )

        self.assertEqual(len(ticket.action_chunk), 12)
        self.assertEqual(len(ticket.execution_chunk), 4)
        self.assertEqual(profile.inference_period_s, 0.2)

    def test_rejects_wrong_horizon_or_dimension(self) -> None:
        with self.assertRaisesRegex(ValueError, "50 steps"):
            InferenceTicket("inference", 0, "a" * 64, chunk()[:-1], PROFILE)
        with self.assertRaisesRegex(ValueError, "14 values"):
            InferenceTicket(
                "inference", 0, "a" * 64, ((0.0,) * 13,) * 50, PROFILE
            )

    def test_rejects_non_finite_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            InferenceTicket("inference", 0, "a" * 64, chunk(math.nan), PROFILE)


if __name__ == "__main__":
    unittest.main()
