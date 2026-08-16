#!/usr/bin/env bash
set -euo pipefail

ros_distro=${ROS_DISTRO:-jazzy}
station_config=${1:-/workspace/config/station.w3.json}
color_format=${ARX5_CAMERA_COLOR_FORMAT:-yuyv}
reliability=${ARX5_CAMERA_RELIABILITY:-reliable}
frame_timeout_ms=${ARX5_CAMERA_FRAME_TIMEOUT_MS:-5000}

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/collection_ws/install/setup.bash
set -u

exec ros2 launch arx5_camera_source three_cameras.launch.py \
  "station_config:=${station_config}" \
  "color_format:=${color_format}" \
  "reliability:=${reliability}" \
  "frame_timeout_ms:=${frame_timeout_ms}"
