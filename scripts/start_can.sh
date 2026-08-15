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

state_dir=${ARX5_CAN_STATE_DIR:-/run/arx5-dual-collection}
state_file=${state_dir}/slcand.state
mkdir -p "${state_dir}"

if [[ -s ${state_file} ]]; then
  echo "error: state file already exists: ${state_file}" >&2
  echo "run scripts/stop_can.sh first" >&2
  exit 1
fi

resolve_tty() {
  local serial=$1
  local tty_path
  local detected_serial

  for tty_path in /dev/ttyACM*; do
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

  slcand -F -o -c -f -s8 "${tty_path}" "${interface_name}" &
  process_id=$!
  started_pids+=("${process_id}")

  for _ in {1..30}; do
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

start_interface left 0045002B5330530320323656 can1
start_interface right 004E002E5330530320323656 can3

trap - ERR
echo "CAN interfaces are ready; no arm motion or mode command was sent."
