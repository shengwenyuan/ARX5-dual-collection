from __future__ import annotations

import unittest

import numpy as np

from arx5_collection.collection.dagger.training_time_rtc_policy import (
    prepare_model_prefix,
)


class TrainingTimeRtcPolicyTest(unittest.TestCase):
    def test_pads_robot_prefix_then_uses_checkpoint_input_transform(self) -> None:
        seen = {}

        def transform(payload):
            seen.update(payload)
            return {**payload, "actions": payload["actions"] + 1.0}

        observation, prefix, delay = prepare_model_prefix(
            {"state": np.zeros(14, dtype=np.float32), "images": {}},
            {
                "estimated_delay_steps": 2,
                "action_prefix": np.full((2, 14), 0.5, dtype=np.float32),
            },
            transform,
            action_horizon=5,
            action_dimension=14,
            max_delay_steps=3,
            numpy_module=np,
        )

        self.assertEqual(delay, 2)
        self.assertNotIn("actions", observation)
        self.assertEqual(prefix.shape, (5, 14))
        np.testing.assert_allclose(prefix[:2], 1.5)
        np.testing.assert_allclose(prefix[2:], 1.0)
        np.testing.assert_allclose(seen["actions"][:2], 0.5)

    def test_rejects_extra_protocol_fields_and_wrong_prefix_shape(self) -> None:
        common = dict(
            observation={"state": np.zeros(14)},
            input_transform=lambda row: row,
            action_horizon=5,
            action_dimension=14,
            max_delay_steps=3,
            numpy_module=np,
        )
        with self.assertRaisesRegex(ValueError, "minimal contract"):
            prepare_model_prefix(
                rtc={
                    "estimated_delay_steps": 1,
                    "action_prefix": [[0.0] * 14],
                    "queue_remaining": 10,
                },
                **common,
            )
        with self.assertRaisesRegex(ValueError, "shape"):
            prepare_model_prefix(
                rtc={"estimated_delay_steps": 2, "action_prefix": [[0.0] * 14]},
                **common,
            )


if __name__ == "__main__":
    unittest.main()
