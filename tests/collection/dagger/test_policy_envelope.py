from __future__ import annotations

import unittest

from arx5_collection.collection.dagger.policy_envelope import CorrelatedPolicyEnvelope


class Policy:
    def __init__(self) -> None:
        self.observation = None

    def infer(self, observation):
        self.observation = observation
        return {"actions": [[0.0] * 14] * 50}

    def infer_rtc(self, observation, rtc):
        self.observation = observation
        self.rtc = rtc
        return {"actions": [[0.0] * 14] * 50}


class PolicyEnvelopeTest(unittest.TestCase):
    def test_echoes_authority_identity_around_official_observation(self) -> None:
        policy = Policy()
        times = iter((100, 250))
        envelope = CorrelatedPolicyEnvelope(policy, "a" * 64, lambda: next(times))

        response = envelope.infer(
            {
                "session_id": "session-1",
                "episode_id": "episode-1",
                "control_epoch": 3,
                "inference_id": "inference-1",
                "checkpoint_sha256": "a" * 64,
                "prompt": "Stacking paper cups",
                "observation": {"state": [0.0] * 14, "images": {}},
            }
        )

        self.assertEqual(response["session_id"], "session-1")
        self.assertEqual(response["episode_id"], "episode-1")
        self.assertEqual(response["control_epoch"], 3)
        self.assertEqual(response["inference_id"], "inference-1")
        self.assertEqual(response["checkpoint_sha256"], "a" * 64)
        self.assertEqual(response["started_at_ns"], 100)
        self.assertEqual(response["completed_at_ns"], 250)
        self.assertEqual(policy.observation["prompt"], "Stacking paper cups")

    def test_rejects_checkpoint_mismatch_before_inference(self) -> None:
        policy = Policy()
        envelope = CorrelatedPolicyEnvelope(policy, "a" * 64, lambda: 0)
        with self.assertRaisesRegex(ValueError, "does not match"):
            envelope.infer(
                {
                    "session_id": "session-1",
                    "episode_id": "episode-1",
                    "control_epoch": 0,
                    "inference_id": "inference-1",
                    "checkpoint_sha256": "b" * 64,
                    "prompt": "task",
                    "observation": {},
                }
            )
        self.assertIsNone(policy.observation)

    def test_passes_only_rtc_context_beside_the_official_observation(self) -> None:
        policy = Policy()
        envelope = CorrelatedPolicyEnvelope(policy, "a" * 64, lambda: 10)
        rtc = {"estimated_delay_steps": 2, "action_prefix": [[0.0] * 14] * 2}

        envelope.infer(
            {
                "session_id": "session-1",
                "episode_id": "episode-1",
                "control_epoch": 0,
                "inference_id": "inference-1",
                "checkpoint_sha256": "a" * 64,
                "prompt": "task",
                "observation": {"state": [0.0] * 14, "images": {}},
                "rtc": rtc,
            }
        )

        self.assertEqual(policy.rtc, rtc)
        self.assertNotIn("rtc", policy.observation)


if __name__ == "__main__":
    unittest.main()
