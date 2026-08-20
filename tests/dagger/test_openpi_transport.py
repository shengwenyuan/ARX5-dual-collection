from __future__ import annotations

import unittest

from arx5_collection.dagger.observation import (
    Pi05Observation,
    RgbFrame,
)
from arx5_collection.dagger.openpi_transport import (
    policy_request_to_wire,
    policy_response_from_wire,
    validate_rtc_server_metadata,
)
from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.dagger.policy_client import Pi05PolicyRequest
from arx5_collection.dagger.policy_client import RtcPolicyContext


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
    float32 = "float32"
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

    def test_wire_rtc_context_is_minimal_float32_prefix(self) -> None:
        request = self.request()
        request = Pi05PolicyRequest(
            request.session_id,
            request.episode_id,
            request.control_epoch,
            request.inference_id,
            request.checkpoint_sha256,
            request.prompt,
            request.observation,
            RtcPolicyContext(2, ((0.0,) * 14, (1.0,) * 14)),
        )

        wire = policy_request_to_wire(request, Numpy)

        self.assertEqual(set(wire["rtc"]), {"estimated_delay_steps", "action_prefix"})
        self.assertEqual(wire["rtc"]["estimated_delay_steps"], 2)
        self.assertEqual(wire["rtc"]["action_prefix"].value[1][0], 1.0)

    def test_rtc_server_handshake_rejects_profile_drift(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        profile = DaggerCollectorSettings.load(
            root / "config" / "dagger.pi05-stacking-v3-rtc.toml"
        ).checkpoint_profile
        metadata = {
            "policy_type": profile.policy_type,
            "action_horizon": profile.execution.action_chunk_size,
            "action_dimension": profile.execution.action_dimension,
            "control_rate_hz": profile.execution.control_rate_hz,
            "max_delay_steps": profile.max_delay_steps,
            "flow_steps": profile.flow_steps,
            "action_semantics": profile.action_semantics,
            "prefix_mode": profile.prefix_mode,
            "hard_prefix_tolerance": profile.hard_prefix_tolerance,
            "model_action_dimension": profile.model_action_dimension,
            "gripper_normalization": profile.gripper_normalization,
            "input_width": profile.input.width,
            "input_height": profile.input.height,
            "input_channels": profile.input.channels,
            "input_layout": profile.input.layout,
            "input_color": profile.input.color,
            "input_dtype": profile.input.dtype,
            "input_resize": profile.input.resize,
            "input_crop": profile.input.crop,
            "input_pad": profile.input.pad,
            "model_input_width": profile.input.model_width,
            "model_input_height": profile.input.model_height,
            "model_input_resize": profile.input.model_resize,
            "camera_high_source": profile.input.camera_high_source,
            "camera_left_wrist_source": profile.input.camera_left_wrist_source,
            "camera_right_wrist_source": profile.input.camera_right_wrist_source,
        }
        validate_rtc_server_metadata(metadata, profile)
        metadata["control_rate_hz"] = 30.0
        with self.assertRaisesRegex(RuntimeError, "profile mismatch"):
            validate_rtc_server_metadata(metadata, profile)


if __name__ == "__main__":
    unittest.main()
