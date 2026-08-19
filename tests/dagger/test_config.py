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

[collector]
server_host = "127.0.0.1"
server_port = 8000
inference_timeout_s = 12.5

[observation]
max_camera_span_ms = 40.0
max_arm_age_ms = 2.0
max_snapshot_age_ms = 100.0
service_timeout_ms = 250.0

[gripper]
left_open_raw = -3.0
left_closed_raw = 0.0
right_open_raw = -3.0
right_closed_raw = 0.0
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
        self.assertEqual(collector.snapshot_service_timeout_s, 0.25)


if __name__ == "__main__":
    unittest.main()
