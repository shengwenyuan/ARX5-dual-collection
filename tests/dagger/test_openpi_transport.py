from __future__ import annotations

import unittest

from arx5_collection.dagger.observation import (
    Pi05Observation,
    RgbFrame,
)
from arx5_collection.dagger.openpi_transport import (
    policy_request_to_wire,
    policy_response_from_wire,
)
from arx5_collection.dagger.policy_client import Pi05PolicyRequest


RGB = b"\x00" * (640 * 360 * 3)


class Array:
    def __init__(self, value):
        self.value = value
        self.shape = None

    def reshape(self, *shape):
        self.shape = shape
        return self

    def transpose(self, *axes):
        self.shape = tuple(self.shape[index] for index in axes)
        return self


class Numpy:
    uint8 = "uint8"
    float64 = "float64"

    @staticmethod
    def asarray(value, dtype):
        return Array(tuple(value))

    @staticmethod
    def frombuffer(value, dtype):
        return Array(value)


class OpenPiTransportContractTest(unittest.TestCase):
    def request(self) -> Pi05PolicyRequest:
        frame = RgbFrame(RGB, 100)
        return Pi05PolicyRequest(
            "session-1",
            "episode-1",
            2,
            "inference-1",
            "a" * 64,
            "Stacking paper cups",
            Pi05Observation((0.0,) * 14, frame, frame, frame, 100),
        )

    def test_wire_request_keeps_envelope_and_official_image_names(self) -> None:
        wire = policy_request_to_wire(self.request(), Numpy)
        self.assertEqual(wire["session_id"], "session-1")
        self.assertEqual(wire["control_epoch"], 2)
        self.assertEqual(
            set(wire["observation"]["images"]),
            {"cam_high", "cam_left_wrist", "cam_right_wrist"},
        )
        self.assertEqual(wire["observation"]["images"]["cam_high"].shape, (3, 360, 640))

    def test_wire_response_becomes_validated_policy_response(self) -> None:
        response = policy_response_from_wire(
            {
                "session_id": "session-1",
                "episode_id": "episode-1",
                "control_epoch": 2,
                "inference_id": "inference-1",
                "checkpoint_sha256": "a" * 64,
                "actions": [[0.0] * 14] * 50,
                "started_at_ns": 10,
                "completed_at_ns": 20,
            }
        )
        self.assertEqual(response.episode_id, "episode-1")
        self.assertEqual(len(response.action_chunk), 50)


if __name__ == "__main__":
    unittest.main()
