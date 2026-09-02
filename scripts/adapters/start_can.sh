#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "error: run as root" >&2
  exit 1
fi

for command_name in ip slcand udevadm; do
  command -v "${command_name}" >/dev/null || {
    echo "error: missing command: ${command_name}" >&2
    exit 1
  }
done

mapfile -t runtime_settings < <(python3 -c 'from math import ceil; from arx5_collection.collection.environment import ENVIRONMENT; print(ENVIRONMENT.paths.can_state_dir); print(ENVIRONMENT.paths.device_root); print(ENVIRONMENT.system.can_speed); print(ceil(ENVIRONMENT.system.can_startup_timeout_s / 0.1))')
state_dir=${runtime_settings[0]}
device_root=${runtime_settings[1]}
can_speed=${runtime_settings[2]}
startup_attempts=${runtime_settings[3]}
state_file=${state_dir}/slcand.state
mkdir -p "${state_dir}"

if [[ -s ${state_file} ]]; then
  echo "error: state file already exists: ${state_file}" >&2
  echo "run scripts/adapters/stop_can.sh first" >&2
  exit 1
fi

resolve_tty() {
  local serial=$1
  local tty_path
  local detected_serial

  for tty_path in "${device_root}"/ttyACM*; do
    [[ -e ${tty_path} ]] || continue
    detected_serial=$(udevadm info --query=property --name="${tty_path}" \
      | sed -n 's/^ID_SERIAL_SHORT=//p' | head -n 1)
    if [[ ${detected_serial} == "${serial}" ]]; then
      printf '%s\n' "${tty_path}"
      return 0
    fi
  done

  return 1
}

started_pids=()
cleanup_on_error() {
  local process_id
  for process_id in "${started_pids[@]}"; do
    kill "${process_id}" 2>/dev/null || true
  done
  rm -f "${state_file}"
}
trap cleanup_on_error ERR

start_interface() {
  local label=$1
  local serial=$2
  local interface_name=$3
  local tty_path
  local process_id

  tty_path=$(resolve_tty "${serial}") || {
    echo "error: ${label} USB2CAN serial not found: ${serial}" >&2
    return 1
  }

  if ip link show "${interface_name}" >/dev/null 2>&1; then
    echo "error: interface already exists: ${interface_name}" >&2
    return 1
  fi

  if command -v fuser >/dev/null && fuser "${tty_path}" >/dev/null 2>&1; then
    echo "error: device is already in use: ${tty_path}" >&2
    return 1
  fi

  slcand -F -o -c -f "-${can_speed}" "${tty_path}" "${interface_name}" &
  process_id=$!
  started_pids+=("${process_id}")

  for ((attempt = 0; attempt < startup_attempts; attempt++)); do
    if ip link show "${interface_name}" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done

  ip link show "${interface_name}" >/dev/null 2>&1 || {
    echo "error: slcand did not create ${interface_name}" >&2
    return 1
  }

  ip link set "${interface_name}" up
  printf '%s|%s|%s|%s\n' \
    "${label}" "${process_id}" "${tty_path}" "${interface_name}" >>"${state_file}"
  echo "started ${label}: ${tty_path} -> ${interface_name} (pid ${process_id})"
}

while IFS='|' read -r role serial interface_name; do
  start_interface "${role}" "${serial}" "${interface_name}"
done < <(python3 -c 'from arx5_collection.collection.environment import ENVIRONMENT; from arx5_collection.collection.runtime.config import load_station_config; station = load_station_config(ENVIRONMENT.paths.station_config); [print(f"{arm.role}|{arm.usb_serial}|{arm.can_interface}") for arm in station.arms]')

trap - ERR
echo "CAN interfaces are ready; no arm motion or mode command was sent."
