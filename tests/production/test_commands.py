from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.production.config import CameraConfig
from arx5_collection.production.cli import build_parser, load_configured_station
from arx5_collection.production.processes import CameraSnapshotConfig, RosCommandSet
from arx5_collection.production.profiles import (
    DAGGER_ARM_PROFILE,
    TEACHING_ARM_PROFILE,
)


class RosCommandSetTest(unittest.TestCase):
    def test_missing_station_points_to_single_initialization_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "arx5-collect station configure"):
            load_configured_station(Path("/definitely/missing/station.json"))

    def test_station_domain_migration_command_is_explicit(self) -> None:
        args = build_parser().parse_args(["station", "set-ros-domain-id", "31"])
        self.assertEqual(args.station_command, "set-ros-domain-id")
        self.assertEqual(args.ros_domain_id, 31)

    def test_unified_camera_argv_is_explicit_and_preserves_serials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = RosCommandSet(Path(directory)).d405_source(
                (
                    CameraConfig("left", "camera-left-serial"),
                    CameraConfig("right", "camera-right-serial"),
                    CameraConfig("overview", "camera-overview-serial"),
                ),
                CameraSnapshotConfig(
                    40.0,
                    2.0,
                    100.0,
                    640,
                    360,
                    Path("/dev/shm/arx5-vla-snapshot-31"),
                    Path("/tmp/arx5-vla-snapshot-31.sock"),
                ),
            )
        self.assertEqual(
            command.spec.argv[:4],
            ("ros2", "run", "arx5_d405_source_cpp", "multi_d405_source"),
        )
        self.assertEqual(command.spec.name, "d405-source")
        self.assertIn("serial_left:='camera-left-serial'", command.spec.argv)
        self.assertIn("serial_overview:='camera-overview-serial'", command.spec.argv)
        self.assertIn("serial_right:='camera-right-serial'", command.spec.argv)
        self.assertIn("enable_snapshot_ipc:=true", command.spec.argv)
        self.assertIn("width:=848", command.spec.argv)
        self.assertIn("height:=480", command.spec.argv)
        self.assertIn("max_camera_span_ms:=40.0", command.spec.argv)
        self.assertIn("snapshot_width:=640", command.spec.argv)
        self.assertIn("snapshot_height:=360", command.spec.argv)
        self.assertIn(
            "snapshot_arena_path:='/dev/shm/arx5-vla-snapshot-31'",
            command.spec.argv,
        )
        self.assertIn(
            "snapshot_socket_path:='/tmp/arx5-vla-snapshot-31.sock'",
            command.spec.argv,
        )
        self.assertNotIn("bash", command.spec.argv)

    def test_arx_uses_frozen_official_v2_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = RosCommandSet(Path(directory)).arx5_controller(
                TEACHING_ARM_PROFILE
            )
            dagger = RosCommandSet(Path(directory)).arx5_controller(
                DAGGER_ARM_PROFILE
            )
        self.assertEqual(command.spec.argv[0:2], ("ros2", "launch"))
        self.assertTrue(command.spec.argv[2].endswith("/x5_v2/v2_collect.launch.py"))
        self.assertTrue(
            dagger.spec.argv[2].endswith("/x5_v2/v2_joint_control.launch.py")
        )

    def test_arm_adapter_profile_changes_only_vendor_input_topics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            commands = RosCommandSet(Path(directory))
            teaching = commands.arm_state_adapter(TEACHING_ARM_PROFILE)
            dagger = commands.arm_state_adapter(DAGGER_ARM_PROFILE)

        self.assertIn(
            "left_input_topic:=/arm_master_l_status", teaching.spec.argv
        )
        self.assertIn(
            "right_input_topic:=/arm_master_r_status", teaching.spec.argv
        )
        self.assertIn(
            "left_input_topic:=/arm_slave_l_status", dagger.spec.argv
        )
        self.assertIn(
            "right_input_topic:=/arm_slave_r_status", dagger.spec.argv
        )
        self.assertNotIn("/embodiments/left_arm/state", dagger.spec.argv)


if __name__ == "__main__":
    unittest.main()
