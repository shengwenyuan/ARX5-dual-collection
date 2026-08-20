from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.dagger.policy_client import Pi05PolicyRequest, Pi05PolicyResponse
from arx5_collection.dagger.policy_probe import run_rtc_policy_probe


CONFIG = Path(__file__).parents[2] / "config" / "dagger.pi05-stacking-v3-rtc.toml"


class FakeTransport:
    def __init__(self, prefix_error: float = 0.0) -> None:
        self.requests: list[Pi05PolicyRequest] = []
        self.prefix_error = prefix_error

    def infer(self, request: Pi05PolicyRequest) -> Pi05PolicyResponse:
        self.requests.append(request)
        actions = [[float(index)] * 14 for index in range(50)]
        if request.rtc is not None:
            actions[: len(request.rtc.action_prefix)] = [
                list(row) for row in request.rtc.action_prefix
            ]
            actions[0][0] += self.prefix_error
        return Pi05PolicyResponse(
            session_id=request.session_id,
            episode_id=request.episode_id,
            control_epoch=request.control_epoch,
            inference_id=request.inference_id,
            checkpoint_sha256=request.checkpoint_sha256,
            action_chunk=tuple(tuple(row) for row in actions),
            started_at_ns=1,
            completed_at_ns=2,
        )


class RtcPolicyProbeTest(unittest.TestCase):
    def test_exercises_bootstrap_then_minimal_rtc_prefix(self) -> None:
        settings = DaggerCollectorSettings.load(CONFIG)
        transport = FakeTransport()

        result = run_rtc_policy_probe(settings, transport)

        self.assertEqual(len(transport.requests), 2)
        self.assertIsNone(transport.requests[0].rtc)
        self.assertEqual(transport.requests[1].rtc.estimated_delay_steps, 3)
        self.assertEqual(len(transport.requests[1].rtc.action_prefix), 3)
        self.assertEqual(result.prefix_max_error, 0.0)

    def test_rejects_hard_prefix_drift(self) -> None:
        settings = DaggerCollectorSettings.load(CONFIG)
        transport = FakeTransport(prefix_error=0.001)

        with self.assertRaisesRegex(RuntimeError, "hard-prefix"):
            run_rtc_policy_probe(settings, transport)

    def test_rejects_non_rtc_profile(self) -> None:
        settings = DaggerCollectorSettings.load(CONFIG)
        settings = replace(
            settings,
            checkpoint_profile=replace(
                settings.checkpoint_profile,
                policy_type="sequential",
                max_delay_steps=0,
                prefix_mode="none",
            ),
            rtc_rollout=None,
        )

        with self.assertRaisesRegex(ValueError, "training-time RTC"):
            run_rtc_policy_probe(settings, FakeTransport())


if __name__ == "__main__":
    unittest.main()
