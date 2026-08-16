from __future__ import annotations

from collections.abc import Callable
from time import monotonic

import rclpy
from arx5_arm_msg.msg import RobotStatus
from arx5_collection_interfaces.msg import ArmState, StreamStatus
from arx5_monitoring.reporter import StreamStatusReporter
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)

from arx5_arm_adapter.mapping import map_robot_status_values


ARMS = (
    ("left", "/arm_master_l_status", "/embodiments/left_arm/state"),
    ("right", "/arm_master_r_status", "/embodiments/right_arm/state"),
)


class ArmStateAdapter(Node):
    def __init__(self) -> None:
        super().__init__("arm_state_adapter")
        status_period_s = float(
            self.declare_parameter("status_period_s", 1.0).value
        )
        if status_period_s <= 0:
            raise ValueError("status_period_s must be positive")
        data_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1000,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        status_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=32,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self._status_publisher = self.create_publisher(
            StreamStatus, "/monitoring/stream_status", status_qos
        )
        self._arm_publishers = {}
        self._arm_subscriptions = []
        self._status_reporters = {}
        for arm, input_topic, output_topic in ARMS:
            publisher = self.create_publisher(ArmState, output_topic, data_qos)
            self._arm_publishers[arm] = publisher
            reporter = StreamStatusReporter(
                self,
                self._status_publisher,
                f"{arm}_arm_state",
                output_topic,
            )
            self._status_reporters[arm] = reporter
            callback = self._callback_for(publisher, reporter)
            self._arm_subscriptions.append(
                self.create_subscription(
                    RobotStatus, input_topic, callback, data_qos
                )
            )
            self.get_logger().info(f"mapping {input_topic} -> {output_topic}")
        self._status_timer = self.create_timer(
            status_period_s,
            self._publish_status,
        )

    @staticmethod
    def _callback_for(
        publisher: object,
        reporter: StreamStatusReporter,
    ) -> Callable[[RobotStatus], None]:
        def callback(source: RobotStatus) -> None:
            values = map_robot_status_values(
                source.end_pos,
                source.joint_pos,
                source.joint_vel,
                source.joint_cur,
            )
            output = ArmState()
            output.header = source.header
            output.eef_xyzrpy = values.eef_xyzrpy
            output.joint_positions = values.joint_positions
            output.joint_velocities = values.joint_velocities
            output.joint_currents = values.joint_currents
            output.gripper_position = values.gripper_position
            output.gripper_velocity = values.gripper_velocity
            output.gripper_current = values.gripper_current
            publisher.publish(output)
            message_stamp_ns = (
                int(source.header.stamp.sec) * 1_000_000_000
                + int(source.header.stamp.nanosec)
            )
            reporter.observe(message_stamp_ns, monotonic())

        return callback

    def _publish_status(self) -> None:
        now_s = monotonic()
        for reporter in self._status_reporters.values():
            reporter.publish(now_s)


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = ArmStateAdapter()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
