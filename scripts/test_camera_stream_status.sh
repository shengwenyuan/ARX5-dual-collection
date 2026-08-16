#!/usr/bin/env bash
set -euo pipefail

if (( $# != 3 )); then
  echo "usage: $0 STATION_CONFIG OUTPUT_BAG DURATION_S" >&2
  exit 2
fi

station_config=$1
output_bag=$2
duration_s=$3
ros_distro=${ROS_DISTRO:-jazzy}

[[ -r ${station_config} ]] || {
  echo "error: station config is not readable: ${station_config}" >&2
  exit 1
}
[[ ! -e ${output_bag} ]] || {
  echo "error: output path already exists: ${output_bag}" >&2
  exit 1
}
[[ ${duration_s} =~ ^[1-9][0-9]*$ ]] || {
  echo "error: duration must be a positive integer" >&2
  exit 1
}
mkdir -p "$(dirname "${output_bag}")"

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/arx_ws/install/setup.bash
source /opt/collection_ws/install/setup.bash
set -u

camera_pid=
recorder_pid=
stop_group() {
  local pid=$1
  kill -INT -- "-${pid}" 2>/dev/null || true
  for _ in $(seq 1 25); do
    kill -0 "${pid}" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -TERM -- "-${pid}" 2>/dev/null || true
}
cleanup() {
  set +e
  if [[ -n ${recorder_pid} ]]; then
    stop_group "${recorder_pid}"
    wait "${recorder_pid}" 2>/dev/null
  fi
  if [[ -n ${camera_pid} ]]; then
    stop_group "${camera_pid}"
    wait "${camera_pid}" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

setsid /opt/arx5-dual-collection/scripts/run_d405_sources.sh "${station_config}" \
  >"${output_bag}.camera.log" 2>&1 &
camera_pid=$!

for _ in $(seq 1 100); do
  publishers=$(ros2 topic info /monitoring/stream_status 2>/dev/null \
    | awk '/Publisher count:/ {print $3}' || true)
  [[ ${publishers} == 3 ]] && break
  sleep 0.2
done
if [[ ${publishers:-0} != 3 ]]; then
  echo "error: expected three camera telemetry publishers" >&2
  exit 1
fi

setsid ros2 bag record --storage mcap --output "${output_bag}" --topics \
  /monitoring/stream_status >"${output_bag}.record.log" 2>&1 &
recorder_pid=$!

for _ in $(seq 1 50); do
  subscribers=$(ros2 topic info /monitoring/stream_status 2>/dev/null \
    | awk '/Subscription count:/ {print $3}' || true)
  [[ ${subscribers} == 1 ]] && break
  sleep 0.2
done
if [[ ${subscribers:-0} != 1 ]]; then
  echo "error: telemetry recorder did not subscribe" >&2
  exit 1
fi

sleep "${duration_s}"
stop_group "${recorder_pid}"
wait "${recorder_pid}"
recorder_pid=
stop_group "${camera_pid}"
wait "${camera_pid}"
camera_pid=
trap - EXIT INT TERM

echo "Camera telemetry recorded: ${output_bag}"
