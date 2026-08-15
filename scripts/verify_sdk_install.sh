#!/usr/bin/env bash
set -euo pipefail

ros_distro=${ROS_DISTRO:-jazzy}
expected_realsense=${EXPECTED_REALSENSE_VERSION:-2.54.2}

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/arx_ws/install/setup.bash
set -u

actual_realsense=$(pkg-config --modversion realsense2)
if [[ ${actual_realsense} != "${expected_realsense}" ]]; then
  echo "error: expected librealsense ${expected_realsense}, found ${actual_realsense}" >&2
  exit 1
fi

python3 -c 'import pyrealsense2'

for package_name in arx_x5_controller arx5_arm_msg arm_control rosbag2_storage_mcap; do
  ros2 pkg prefix "${package_name}" >/dev/null
done

controller=/opt/arx_ws/install/lib/arx_x5_controller/X5Controller
[[ -x ${controller} ]] || {
  echo "error: missing ARX5 controller: ${controller}" >&2
  exit 1
}

if ldd "${controller}" | grep -q 'not found'; then
  echo "error: ARX5 controller has unresolved shared libraries" >&2
  ldd "${controller}" >&2
  exit 1
fi

echo "SDK verification passed: librealsense ${actual_realsense}, ARX_X5 ${ARX_X5_REF:-main}, ROS 2 ${ros_distro}."
