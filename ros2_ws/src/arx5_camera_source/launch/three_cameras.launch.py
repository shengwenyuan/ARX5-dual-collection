from __future__ import annotations

from pathlib import Path

from launch import LaunchContext, LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from arx5_camera_source.camera_config import load_station_cameras


def _launch_cameras(context: LaunchContext) -> list[Node]:
    config_path = Path(LaunchConfiguration("station_config").perform(context))
    reliability = LaunchConfiguration("reliability").perform(context)
    frame_timeout_ms = int(LaunchConfiguration("frame_timeout_ms").perform(context))
    return [
        Node(
            package="arx5_camera_source",
            executable="d405_source",
            namespace=spec.namespace,
            name=f"d405_source_{spec.role}",
            output="screen",
            emulate_tty=True,
            parameters=[
                {
                    "camera_name": spec.role,
                    "serial": spec.serial,
                    "width": 848,
                    "height": 480,
                    "fps": 30,
                    "reliability": reliability,
                    "frame_timeout_ms": frame_timeout_ms,
                }
            ],
        )
        for spec in load_station_cameras(config_path)
    ]


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            DeclareLaunchArgument("station_config"),
            DeclareLaunchArgument("reliability", default_value="reliable"),
            DeclareLaunchArgument("frame_timeout_ms", default_value="5000"),
            OpaqueFunction(function=_launch_cameras),
        ]
    )
