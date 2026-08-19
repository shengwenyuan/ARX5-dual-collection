from __future__ import annotations

import unittest
from threading import Event

from arx5_collection.dagger.observation import Pi05Observation, RgbFrame
from arx5_collection.dagger.models import PolicyExecutionProfile
from arx5_collection.dagger.policy_client import (
    AsyncPi05PolicyClient,
    Pi05PolicyRequest,
    Pi05PolicyResponse,
    StalePolicyResponseError,
)


RGB = b"\x00" * 12
ACTION_CHUNK = ((0.0,) * 14,) * 50
EXECUTION = PolicyExecutionProfile(50, 14, 10, 25.0)


class Source:
    def capture(self):
        return object()


class Encoder:
    def encode(self, step) -> Pi05Observation:
        frame = RgbFrame(RGB, 100, width=2, height=2)
        return Pi05Observation((0.0,) * 14, frame, frame, frame, 100)


class Transport:
    def __init__(self) -> None:
        self.requests: list[Pi05PolicyRequest] = []
        self.entered = Event()
        self.release = Event()
        self.block = False

    def infer(self, request: Pi05PolicyRequest) -> Pi05PolicyResponse:
        self.requests.append(request)
        self.entered.set()
        if self.block:
            self.release.wait(1.0)
        return Pi05PolicyResponse(
            request.session_id,
            request.episode_id,
            request.control_epoch,
            request.inference_id,
            request.checkpoint_sha256,
            ACTION_CHUNK,
            10,
            20,
        )


class AsyncPi05PolicyClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = Transport()
        self.client = AsyncPi05PolicyClient(
            "session-1",
            "task",
            "a" * 64,
            Source(),
            Encoder(),
            self.transport,
            EXECUTION,
        )

    def tearDown(self) -> None:
        self.transport.release.set()
        self.client.close()

    def test_submit_is_async_and_correlated(self) -> None:
        future = self.client.submit("episode-1", 0, "request-1")
        ticket = future.result(1.0)

        self.assertEqual(ticket.inference_id, "request-1")
        self.assertEqual(ticket.execution, EXECUTION)
        self.assertEqual(self.transport.requests[0].episode_id, "episode-1")

    def test_old_epoch_response_is_discarded(self) -> None:
        self.transport.block = True
        future = self.client.submit("episode-1", 0, "request-1")
        self.assertTrue(self.transport.entered.wait(1.0))
        self.client.begin_epoch(1)
        self.transport.release.set()

        with self.assertRaises(StalePolicyResponseError):
            future.result(1.0)

    def test_rejects_request_for_inactive_epoch(self) -> None:
        self.client.begin_epoch(2)
        with self.assertRaises(StalePolicyResponseError):
            self.client.submit("episode-1", 1)


if __name__ == "__main__":
    unittest.main()
