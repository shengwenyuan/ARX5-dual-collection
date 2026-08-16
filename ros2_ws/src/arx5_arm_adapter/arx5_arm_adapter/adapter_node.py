from __future__ import annotations

from collections.abc import Callable

import rclpy
from arx5_arm_msg.msg import RobotStatus
from arx5_collection_interfaces.msg import ArmState
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
        qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1000,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self._arm_publishers = {}
        self._arm_subscriptions = []
        for arm, input_topic, output_topic in ARMS:
            publisher = self.create_publisher(ArmState, output_topic, qos)
            self._arm_publishers[arm] = publisher
            callback = self._callback_for(publisher)
            self._arm_subscriptions.append(
                self.create_subscription(RobotStatus, input_topic, callback, qos)
            )
            self.get_logger().info(f"mapping {input_topic} -> {output_topic}")

    @staticmethod
    def _callback_for(publisher: object) -> Callable[[RobotStatus], None]:
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

        return callback


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
