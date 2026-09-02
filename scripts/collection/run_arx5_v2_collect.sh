#!/usr/bin/env bash
set -euo pipefail

ros_distro=${ROS_DISTRO:-jazzy}
mapfile -t settings < <(python3 -c 'from arx5_collection.collection.environment import ENVIRONMENT; from arx5_collection.collection.runtime.profiles import TEACHING_ARM_PROFILE; [print(value) for value in (*ENVIRONMENT.station.provisional_can_interfaces, TEACHING_ARM_PROFILE.controller_launch)]')
set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/arx_ws/install/setup.bash
set -u

for interface_name in "${settings[@]:0:${#settings[@]}-1}"; do
  ip link show "${interface_name}" >/dev/null 2>&1 || {
    echo "error: required CAN interface is missing: ${interface_name}" >&2
    exit 1
  }
  ip link show "${interface_name}" | grep -q 'state UP' || {
    echo "error: required CAN interface is not UP: ${interface_name}" >&2
    exit 1
  }
done

if topic_info=$(ros2 topic info /arx_joy 2>/dev/null); then
  if ! grep -q 'Publisher count: 0' <<<"${topic_info}"; then
    echo "error: /arx_joy has an active publisher; refusing to start ARX5" >&2
    echo "${topic_info}" >&2
    exit 1
  fi
fi

launch_file=${settings[-1]}
[[ -f ${launch_file} ]] || {
  echo "error: missing official ARX5 launch file: ${launch_file}" >&2
  exit 1
}

echo "Starting official ARX5 v2_collect: ${settings[*]:0:${#settings[@]}-1}, remote_master, G_COMPENSATION."
exec ros2 launch "${launch_file}"
