from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.production.config import CameraConfig
from arx5_collection.production.cli import load_configured_station
from arx5_collection.production.processes import RosCommandSet


class RosCommandSetTest(unittest.TestCase):
    def test_missing_station_points_to_single_initialization_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "arx5-collect station configure"):
            load_configured_station(Path("/definitely/missing/station.json"))

    def test_camera_argv_is_explicit_and_preserves_serial_as_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = RosCommandSet(Path(directory)).d405_source(
                CameraConfig("left", "camera-left-serial")
            )
        self.assertEqual(
            command.spec.argv[:4],
            ("ros2", "run", "arx5_camera_source", "d405_source"),
        )
        self.assertIn("__ns:=/sensors/camera_left", command.spec.argv)
        self.assertIn("serial:='camera-left-serial'", command.spec.argv)
        self.assertNotIn("bash", command.spec.argv)

    def test_arx_uses_frozen_official_v2_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = RosCommandSet(Path(directory)).arx5_v2_collect()
        self.assertEqual(command.spec.argv[0:2], ("ros2", "launch"))
        self.assertTrue(command.spec.argv[2].endswith("/x5_v2/v2_collect.launch.py"))


if __name__ == "__main__":
    unittest.main()
