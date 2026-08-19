from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import unittest

try:
    import rclpy
    from arx5_collection_interfaces.msg import ArmState
    from rclpy.context import Context
    from sensor_msgs.msg import Image
except ImportError:
    rclpy = None

from arx5_collection.dagger.observation import ObservationUnavailableError
from arx5_collection.dagger.ros_snapshot import (
    SNAPSHOT_SERVICE,
    RosVlaSnapshotClient,
)


@unittest.skipIf(rclpy is None, "ROS 2 Python dependencies are unavailable")
class RosSnapshotIntegrationTest(unittest.TestCase):
    def test_cpp_source_returns_fresh_five_stream_snapshot(self) -> None:
        if shutil.which("ros2") is None:
            self.skipTest("ros2 executable is unavailable")
        package = subprocess.run(
            ["ros2", "pkg", "prefix", "arx5_vla_snapshot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if package.returncode != 0:
            self.skipTest("arx5_vla_snapshot is not installed")

        source = subprocess.Popen(
            ["ros2", "run", "arx5_vla_snapshot", "vla_snapshot_source"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        context = Context()
        rclpy.init(context=context)
        node = rclpy.create_node("vla_snapshot_test_source", context=context)
        camera_publishers = {
            role: node.create_publisher(Image, topic, 1)
            for role, topic in {
                "left": "/sensors/camera_left/color/image_raw",
                "overview": "/sensors/camera_overview/color/image_raw",
                "right": "/sensors/camera_right/color/image_raw",
            }.items()
        }
        arm_publishers = {
            role: node.create_publisher(ArmState, topic, 1)
            for role, topic in {
                "left": "/embodiments/left_arm/state",
                "right": "/embodiments/right_arm/state",
            }.items()
        }
        client = RosVlaSnapshotClient(timeout_s=1.0, service_name=SNAPSHOT_SERVICE)
        try:
            deadline = time.monotonic() + 5.0
            snapshot = None
            while snapshot is None and time.monotonic() < deadline:
                base_ns = time.time_ns() - 5_000_000
                for publisher in arm_publishers.values():
                    publisher.publish(_arm_message(base_ns + 2_500_000))
                camera_publishers["left"].publish(_image_message(base_ns + 1_000_000))
                camera_publishers["overview"].publish(
                    _image_message(base_ns + 2_000_000)
                )
                camera_publishers["right"].publish(_image_message(base_ns + 3_000_000))
                time.sleep(0.03)
                try:
                    snapshot = client.capture()
                except ObservationUnavailableError:
                    pass
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertLessEqual(
                max(
                    snapshot.camera_left.stamp_ns,
                    snapshot.camera_overview.stamp_ns,
                    snapshot.camera_right.stamp_ns,
                )
                - min(
                    snapshot.camera_left.stamp_ns,
                    snapshot.camera_overview.stamp_ns,
                    snapshot.camera_right.stamp_ns,
                ),
                40_000_000,
            )
        finally:
            client.close()
            node.destroy_node()
            context.shutdown()
            try:
                os.killpg(source.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                source.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                os.killpg(source.pid, signal.SIGKILL)
                source.wait(timeout=2.0)


def _stamp(message, value_ns: int) -> None:
    message.header.stamp.sec = value_ns // 1_000_000_000
    message.header.stamp.nanosec = value_ns % 1_000_000_000


def _image_message(stamp_ns: int):
    message = Image()
    _stamp(message, stamp_ns)
    message.width = 4
    message.height = 4
    message.encoding = "yuyv"
    message.step = 8
    message.data = bytes((16, 128, 16, 128)) * 8
    return message


def _arm_message(stamp_ns: int):
    message = ArmState()
    _stamp(message, stamp_ns)
    message.joint_positions = [0.0] * 6
    message.gripper_position = 0.0
    return message


if __name__ == "__main__":
    unittest.main()
