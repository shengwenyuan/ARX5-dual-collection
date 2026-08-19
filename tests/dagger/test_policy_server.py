from __future__ import annotations

import unittest

from arx5_collection.dagger.policy_server import warm_up_pi05_policy


class Array:
    def __init__(self, shape, dtype) -> None:
        self.shape = shape
        self.dtype = dtype


class Numpy:
    uint8 = "uint8"
    float64 = "float64"

    @staticmethod
    def zeros(shape, dtype):
        return Array(shape, dtype)


class Policy:
    def __init__(self) -> None:
        self.observation = None

    def infer(self, observation):
        self.observation = observation
        return {"actions": [[0.0] * 14] * 50}


class PolicyWarmupTest(unittest.TestCase):
    def test_uses_the_accepted_raw_aloha_shape_before_readiness(self) -> None:
        policy = Policy()

        warm_up_pi05_policy(policy, "Stacking paper cups", Numpy)

        self.assertEqual(policy.observation["state"].shape, 14)
        self.assertEqual(
            policy.observation["images"]["cam_high"].shape,
            (3, 360, 640),
        )
        self.assertEqual(policy.observation["prompt"], "Stacking paper cups")


if __name__ == "__main__":
    unittest.main()
