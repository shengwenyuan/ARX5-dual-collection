#!/usr/bin/env bash
set -euo pipefail

ros_distro=${ROS_DISTRO:-jazzy}
expected_realsense=${EXPECTED_REALSENSE_VERSION:-2.54.2}

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/arx_ws/install/setup.bash
source /opt/collection_ws/install/setup.bash
set -u

actual_realsense=$(pkg-config --modversion realsense2)
if [[ ${actual_realsense} != "${expected_realsense}" ]]; then
  echo "error: expected librealsense ${expected_realsense}, found ${actual_realsense}" >&2
  exit 1
fi

python3 -c 'import pyrealsense2'
python3 -c 'from arx5_collection.ros2_adapters import RosStreamMonitor, RosbagRecordingBackend'
command -v arx5-collect >/dev/null
arx5-collect --help >/dev/null

for package_name in \
  arx_x5_controller \
  arx5_arm_msg \
  arm_control \
  arx5_camera_source \
  arx5_collection_interfaces \
  arx5_monitoring \
  arx5_arm_adapter \
  rosbag2_storage_mcap; do
  ros2 pkg prefix "${package_name}" >/dev/null
done

ros2 pkg executables arx5_camera_source | grep -q 'd405_source'
ros2 pkg executables arx5_arm_adapter | grep -q 'arm_state_adapter'
ros2 interface show arx5_collection_interfaces/msg/ArmState | grep -q 'gripper_current'
ros2 interface show arx5_collection_interfaces/msg/StreamStatus | grep -q 'non_monotonic_count'

fastdds_profile=${FASTDDS_DEFAULT_PROFILES_FILE:-}
[[ -r ${fastdds_profile} ]] || {
  echo "error: missing Fast DDS profile: ${fastdds_profile:-unset}" >&2
  exit 1
}

shm_size_bytes=$(df --block-size=1 --output=size /dev/shm | tail -n 1)
if (( shm_size_bytes < 1073741824 )); then
  echo "error: /dev/shm must be at least 1 GiB, found ${shm_size_bytes} bytes" >&2
  exit 1
fi

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
