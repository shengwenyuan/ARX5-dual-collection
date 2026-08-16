from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            Node(
                package="arx5_arm_adapter",
                executable="arm_state_adapter",
                output="screen",
                emulate_tty=True,
            )
        ]
    )
