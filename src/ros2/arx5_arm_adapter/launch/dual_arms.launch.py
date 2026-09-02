from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    names = (
        "left_input_topic",
        "right_input_topic",
        "left_output_topic",
        "right_output_topic",
        "stream_status_topic",
        "stream_status_period_s",
    )
    return LaunchDescription(
        [DeclareLaunchArgument(name) for name in names]
        + [
            Node(
                package="arx5_arm_adapter",
                executable="arm_state_adapter",
                output="screen",
                emulate_tty=True,
                parameters=[{name: LaunchConfiguration(name) for name in names}],
            )
        ]
    )
