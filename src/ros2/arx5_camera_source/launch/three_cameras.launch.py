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
    width = int(LaunchConfiguration("width").perform(context))
    height = int(LaunchConfiguration("height").perform(context))
    fps = int(LaunchConfiguration("fps").perform(context))
    stream_status_topic = LaunchConfiguration("stream_status_topic").perform(context)
    stream_status_period_s = float(
        LaunchConfiguration("stream_status_period_s").perform(context)
    )
    return [
        Node(
            package="arx5_camera_source",
            executable="d405_source",
            name=f"d405_source_{spec.role}",
            output="screen",
            emulate_tty=True,
            parameters=[
                {
                    "camera_name": spec.role,
                    "serial": spec.serial,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "reliability": reliability,
                    "frame_timeout_ms": frame_timeout_ms,
                    "color_topic": LaunchConfiguration(
                        f"color_topic_{spec.role}"
                    ).perform(context),
                    "depth_topic": LaunchConfiguration(
                        f"depth_topic_{spec.role}"
                    ).perform(context),
                    "stream_status_topic": stream_status_topic,
                    "stream_status_period_s": stream_status_period_s,
                }
            ],
        )
        for spec in load_station_cameras(config_path)
    ]


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            DeclareLaunchArgument("station_config"),
            DeclareLaunchArgument("width"),
            DeclareLaunchArgument("height"),
            DeclareLaunchArgument("fps"),
            DeclareLaunchArgument("reliability"),
            DeclareLaunchArgument("frame_timeout_ms"),
            DeclareLaunchArgument("stream_status_topic"),
            DeclareLaunchArgument("stream_status_period_s"),
            DeclareLaunchArgument("color_topic_left"),
            DeclareLaunchArgument("depth_topic_left"),
            DeclareLaunchArgument("color_topic_right"),
            DeclareLaunchArgument("depth_topic_right"),
            DeclareLaunchArgument("color_topic_overview"),
            DeclareLaunchArgument("depth_topic_overview"),
            OpaqueFunction(function=_launch_cameras),
        ]
    )
