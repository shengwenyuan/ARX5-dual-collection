#!/usr/bin/env bash
set -euo pipefail

ros_distro=${ROS_DISTRO:-jazzy}
mapfile -t settings < <(python3 -c 'from arx5_collection.collection.capture import RGBD_STREAMS; from arx5_collection.collection.runtime.profiles import TEACHING_ARM_PROFILE; from arx5_collection.common.specs import RUNTIME_INTERFACE_SPEC; values = (TEACHING_ARM_PROFILE.left_input_topic, TEACHING_ARM_PROFILE.right_input_topic, RGBD_STREAMS["left_arm_state"], RGBD_STREAMS["right_arm_state"], RUNTIME_INTERFACE_SPEC.stream_status_topic, RUNTIME_INTERFACE_SPEC.stream_status_period_s); [print(value) for value in values]')

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/arx_ws/install/setup.bash
source /opt/collection_ws/install/setup.bash
set -u

exec ros2 launch arx5_arm_adapter dual_arms.launch.py \
  "left_input_topic:=${settings[0]}" \
  "right_input_topic:=${settings[1]}" \
  "left_output_topic:=${settings[2]}" \
  "right_output_topic:=${settings[3]}" \
  "stream_status_topic:=${settings[4]}" \
  "stream_status_period_s:=${settings[5]}"
