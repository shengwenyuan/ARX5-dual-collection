#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state_dir=${ARX5_CAN_STATE_DIR:-/run/arx5-dual-collection}
state_file=${state_dir}/slcand.state

cleanup() {
  "${script_dir}/stop_can.sh" || true
}

terminate() {
  exit 0
}

trap cleanup EXIT
trap terminate INT TERM

"${script_dir}/ensure_usbfs_memory.sh"
"${script_dir}/start_can.sh"

while [[ -s ${state_file} ]]; do
  all_alive=true
  while IFS='|' read -r _ process_id _ _; do
    if ! kill -0 "${process_id}" 2>/dev/null; then
      all_alive=false
      break
    fi
  done <"${state_file}"

  if [[ ${all_alive} != true ]]; then
    echo "error: a managed slcand process exited" >&2
    exit 1
  fi
  sleep 1
done
