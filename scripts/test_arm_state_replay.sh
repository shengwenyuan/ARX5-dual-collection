#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )); then
  echo "usage: $0 INPUT_BAG OUTPUT_BAG" >&2
  exit 2
fi

input_bag=$1
output_bag=$2
ros_distro=${ROS_DISTRO:-jazzy}

[[ -d ${input_bag} ]] || {
  echo "error: input bag does not exist: ${input_bag}" >&2
  exit 1
}
[[ ! -e ${output_bag} ]] || {
  echo "error: output path already exists: ${output_bag}" >&2
  exit 1
}
mkdir -p "$(dirname "${output_bag}")"

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/arx_ws/install/setup.bash
source /opt/collection_ws/install/setup.bash
set -u

adapter_pid=
recorder_pid=
player_pid=
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
  if [[ -n ${adapter_pid} ]]; then
    stop_group "${adapter_pid}"
    wait "${adapter_pid}" 2>/dev/null
  fi
  if [[ -n ${player_pid} ]]; then
    stop_group "${player_pid}"
    wait "${player_pid}" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

setsid /opt/arx5-dual-collection/scripts/run_arm_state_adapter.sh \
  >"${output_bag}.adapter.log" 2>&1 &
adapter_pid=$!

for _ in $(seq 1 50); do
  if ros2 topic info /embodiments/left_arm/state 2>/dev/null \
      | grep -q 'Publisher count: 1' \
      && ros2 topic info /embodiments/right_arm/state 2>/dev/null \
      | grep -q 'Publisher count: 1'; then
    break
  fi
  sleep 0.2
done

left_publishers=$(ros2 topic info /embodiments/left_arm/state 2>/dev/null \
  | awk '/Publisher count:/ {print $3}' || true)
right_publishers=$(ros2 topic info /embodiments/right_arm/state 2>/dev/null \
  | awk '/Publisher count:/ {print $3}' || true)
if [[ ${left_publishers} != 1 || ${right_publishers} != 1 ]]; then
  echo "error: adapter did not publish both logical topics" >&2
  exit 1
fi

setsid ros2 bag record --storage mcap --output "${output_bag}" --topics \
  /embodiments/left_arm/state /embodiments/right_arm/state \
  >"${output_bag}.record.log" 2>&1 &
recorder_pid=$!

for _ in $(seq 1 50); do
  if ros2 topic info /embodiments/left_arm/state 2>/dev/null \
      | grep -q 'Subscription count: 1' \
      && ros2 topic info /embodiments/right_arm/state 2>/dev/null \
      | grep -q 'Subscription count: 1'; then
    break
  fi
  sleep 0.2
done

left_subscribers=$(ros2 topic info /embodiments/left_arm/state | awk '/Subscription count:/ {print $3}')
right_subscribers=$(ros2 topic info /embodiments/right_arm/state | awk '/Subscription count:/ {print $3}')
if [[ ${left_subscribers} != 1 || ${right_subscribers} != 1 ]]; then
  echo "error: recorder did not subscribe to both logical topics" >&2
  exit 1
fi

setsid ros2 bag play "${input_bag}" --topics \
  /arm_master_l_status /arm_master_r_status \
  --start-paused --disable-keyboard-controls --wait-for-all-acked 5000 \
  >"${output_bag}.play.log" 2>&1 &
player_pid=$!

for _ in $(seq 1 50); do
  if ros2 topic info /arm_master_l_status 2>/dev/null \
      | grep -q 'Subscription count: 1' \
      && ros2 topic info /arm_master_r_status 2>/dev/null \
      | grep -q 'Subscription count: 1'; then
    break
  fi
  sleep 0.2
done

left_input_subscribers=$(ros2 topic info /arm_master_l_status 2>/dev/null \
  | awk '/Subscription count:/ {print $3}' || true)
right_input_subscribers=$(ros2 topic info /arm_master_r_status 2>/dev/null \
  | awk '/Subscription count:/ {print $3}' || true)
if [[ ${left_input_subscribers} != 1 || ${right_input_subscribers} != 1 ]]; then
  echo "error: bag player did not discover both adapter inputs" >&2
  exit 1
fi

for _ in $(seq 1 50); do
  if ros2 service call \
      /rosbag2_player/is_paused rosbag2_interfaces/srv/IsPaused '{}' 2>/dev/null \
      | grep -q 'paused=True'; then
    break
  fi
  sleep 0.2
done
if ! ros2 service call \
    /rosbag2_player/is_paused rosbag2_interfaces/srv/IsPaused '{}' 2>/dev/null \
    | grep -q 'paused=True'; then
  echo "error: bag player did not enter paused state" >&2
  exit 1
fi

ros2 service call /rosbag2_player/resume rosbag2_interfaces/srv/Resume '{}'
wait "${player_pid}"
player_pid=
sleep 2

stop_group "${recorder_pid}"
wait "${recorder_pid}"
recorder_pid=
stop_group "${adapter_pid}"
wait "${adapter_pid}"
adapter_pid=
trap - EXIT INT TERM

echo "ArmState replay recorded: ${output_bag}"
