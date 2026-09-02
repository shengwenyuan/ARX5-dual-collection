#!/usr/bin/env bash
set -euo pipefail

ros_distro=${ROS_DISTRO:-jazzy}
mapfile -t settings < <(python3 -c 'from arx5_collection.collection.capture import RGBD_STREAMS; from arx5_collection.collection.environment import ENVIRONMENT; from arx5_collection.common.specs import RUNTIME_INTERFACE_SPEC; values = (ENVIRONMENT.paths.station_config, ENVIRONMENT.camera.width, ENVIRONMENT.camera.height, ENVIRONMENT.camera.fps, ENVIRONMENT.camera.reliability, ENVIRONMENT.camera.frame_timeout_ms, RUNTIME_INTERFACE_SPEC.stream_status_topic, RUNTIME_INTERFACE_SPEC.stream_status_period_s, RGBD_STREAMS["camera_left_color"], RGBD_STREAMS["camera_left_aligned_depth"], RGBD_STREAMS["camera_right_color"], RGBD_STREAMS["camera_right_aligned_depth"], RGBD_STREAMS["camera_overview_color"], RGBD_STREAMS["camera_overview_aligned_depth"]); [print(value) for value in values]')
station_config=${1:-${settings[0]}}
width=${settings[1]}
height=${settings[2]}
fps=${settings[3]}
reliability=${settings[4]}
frame_timeout_ms=${settings[5]}
stream_status_topic=${settings[6]}
stream_status_period_s=${settings[7]}

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/collection_ws/install/setup.bash
set -u

exec ros2 launch arx5_camera_source three_cameras.launch.py \
  "station_config:=${station_config}" \
  "width:=${width}" \
  "height:=${height}" \
  "fps:=${fps}" \
  "reliability:=${reliability}" \
  "frame_timeout_ms:=${frame_timeout_ms}" \
  "stream_status_topic:=${stream_status_topic}" \
  "stream_status_period_s:=${stream_status_period_s}" \
  "color_topic_left:=${settings[8]}" \
  "depth_topic_left:=${settings[9]}" \
  "color_topic_right:=${settings[10]}" \
  "depth_topic_right:=${settings[11]}" \
  "color_topic_overview:=${settings[12]}" \
  "depth_topic_overview:=${settings[13]}"
