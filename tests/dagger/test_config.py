from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.dagger.policy_server import PolicyServerSettings


CONFIG = """
[policy]
checkpoint = "/checkpoints/example/9999"
checkpoint_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
repo_id = "local/example"
prompt = "Stacking paper cups"
host = "0.0.0.0"
port = 8000
action_chunk_size = 50
action_dimension = 14
execution_steps = 10

[robot]
profile = "dagger"
rate_hz = 25.0

[collector]
server_host = "127.0.0.1"
server_port = 8000
inference_timeout_s = 12.5

[observation]
max_camera_span_ms = 40.0
max_arm_age_ms = 2.0
max_snapshot_age_ms = 100.0
request_timeout_ms = 250.0

[gripper]
contract = "arx5-gripper-v1"
"""


class DaggerConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "policy.toml"
        self.path.write_text(CONFIG)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_same_config_drives_server_and_collector_identity(self) -> None:
        server = PolicyServerSettings.load(self.path)
        collector = DaggerCollectorSettings.load(self.path)

        self.assertEqual(server.checkpoint, Path("/checkpoints/example/9999"))
        self.assertEqual(server.checkpoint_sha256, collector.checkpoint_sha256)
        self.assertEqual(server.prompt, collector.prompt)
        self.assertEqual(collector.server_host, "127.0.0.1")
        self.assertEqual(collector.inference_timeout_s, 12.5)
        self.assertEqual(collector.observation.max_camera_span_ns, 40_000_000)
        self.assertEqual(collector.observation.max_arm_age_ns, 2_000_000)
        self.assertEqual(collector.observation.max_snapshot_age_ns, 100_000_000)
        self.assertEqual(collector.snapshot_timeout_s, 0.25)
        self.assertEqual(collector.gripper_contract, "arx5-gripper-v1")
        self.assertEqual(collector.gripper_action_offset, 0.0)
        self.assertEqual(collector.grippers.open_value, -3.4)
        self.assertEqual(collector.grippers.closed_value, 0.0)
        self.assertEqual(collector.execution.action_chunk_size, 50)
        self.assertEqual(collector.execution.execution_steps, 10)
        self.assertEqual(collector.execution.control_rate_hz, 25.0)
        self.assertEqual(collector.arm_profile.name, "dagger")
        self.assertEqual(collector.control.safety.max_joint_step_rad, 0.25)
        self.assertEqual(collector.control.safety.max_joint_departure_rad, 1.5)
        self.assertEqual(collector.control.safety.min_policy_gripper, -1.0)
        self.assertEqual(collector.control.safety.max_policy_gripper, 2.0)
        self.assertEqual(collector.control.state_timeout_s, 0.1)
        self.assertEqual(collector.control.policy_wait_timeout_s, 0.5)
        self.assertEqual(collector.control.command_watchdog_s, 0.12)
        self.assertTrue(
            collector.arm_profile.controller_launch.endswith(
                "v2_joint_control.launch.py"
            )
        )
        self.assertEqual(server.execution, collector.execution)

    def test_model_execution_parameters_are_configurable(self) -> None:
        custom = CONFIG.replace(
            "action_chunk_size = 50", "action_chunk_size = 12"
        ).replace("execution_steps = 10", "execution_steps = 4").replace(
            "rate_hz = 25.0", "rate_hz = 20.0"
        )
        self.path.write_text(custom)

        settings = DaggerCollectorSettings.load(self.path)

        self.assertEqual(settings.execution.action_chunk_size, 12)
        self.assertEqual(settings.execution.execution_steps, 4)
        self.assertEqual(settings.execution.control_rate_hz, 20.0)
        self.assertEqual(settings.execution.inference_period_s, 0.2)

    def test_rejects_configurable_gripper_boundaries(self) -> None:
        self.path.write_text(
            CONFIG.replace(
                'contract = "arx5-gripper-v1"',
                "left_open_raw = -3.4\nleft_closed_raw = 0.0",
            )
        )

        with self.assertRaisesRegex(ValueError, "keys must be exactly"):
            DaggerCollectorSettings.load(self.path)

    def test_v3_rtc_numbers_are_loaded_from_one_typed_profile(self) -> None:
        root = Path(__file__).resolve().parents[2]
        path = root / "config" / "dagger.pi05-stacking-v3-rtc.toml"

        collector = DaggerCollectorSettings.load(path)
        server = PolicyServerSettings.load(path)

        self.assertEqual(collector.checkpoint_profile, server.checkpoint_profile)
        self.assertEqual(collector.rtc_rollout, server.rtc_rollout)
        self.assertEqual(collector.execution.action_chunk_size, 50)
        self.assertEqual(collector.execution.action_dimension, 14)
        self.assertEqual(collector.execution.control_rate_hz, 25.0)
        self.assertEqual(collector.checkpoint_profile.max_delay_steps, 10)
        self.assertEqual(collector.checkpoint_profile.flow_steps, 10)
        self.assertEqual(collector.checkpoint_profile.model_action_dimension, 32)
        self.assertEqual(collector.checkpoint_profile.input.width, 640)
        self.assertEqual(collector.checkpoint_profile.input.height, 360)
        self.assertEqual(collector.checkpoint_profile.input.model_width, 224)
        self.assertEqual(collector.checkpoint_profile.input.model_height, 224)
        self.assertEqual(collector.snapshot_timeout_s, 0.2)
        self.assertEqual(collector.control.policy_wait_timeout_s, 0.35)
        self.assertEqual(collector.control.rtc_deadline_margin_s, 0.05)
        assert collector.rtc_rollout is not None
        self.assertEqual(collector.rtc_rollout.prefetch_after_steps, 10)
        self.assertEqual(collector.rtc_rollout.initial_delay_steps, 3)
        self.assertEqual(collector.rtc_rollout.delay_history_size, 10)
        self.assertEqual(
            collector.rtc_rollout.safe_window_steps(
                collector.checkpoint_profile
            ),
            19,
        )

    def test_fold_cloth_20260828_profile_keeps_tested_gripper_offset(self) -> None:
        root = Path(__file__).resolve().parents[2]
        settings = DaggerCollectorSettings.load(
            root / "config" / "dagger.pi05-fold-cloth-20260828-train-rtc.toml"
        )

        self.assertEqual(settings.execution.control_rate_hz, 30.0)
        self.assertEqual(settings.gripper_action_offset, 0.1)
        self.assertEqual(
            settings.checkpoint_sha256,
            "5c2248749f3eaa21f7a6cf2652c3d1306771aa572f1814e51b12c9e58cda38fb",
        )

    def test_rtc_snapshot_timeout_must_precede_request_deadline(self) -> None:
        root = Path(__file__).resolve().parents[2]
        payload = (root / "config" / "dagger.pi05-stacking-v3-rtc.toml").read_text()
        self.path.write_text(
            payload.replace("request_timeout_ms = 200.0", "request_timeout_ms = 350.0")
        )

        with self.assertRaisesRegex(ValueError, "snapshot timeout"):
            DaggerCollectorSettings.load(self.path)

    def test_rtc_request_deadline_reserves_configured_margin(self) -> None:
        root = Path(__file__).resolve().parents[2]
        payload = (root / "config" / "dagger.pi05-stacking-v3-rtc.toml").read_text()
        self.path.write_text(
            payload.replace(
                "policy_wait_timeout_s = 0.35",
                "policy_wait_timeout_s = 0.36",
            )
        )

        with self.assertRaisesRegex(ValueError, "margin exceed"):
            DaggerCollectorSettings.load(self.path)


if __name__ == "__main__":
    unittest.main()
