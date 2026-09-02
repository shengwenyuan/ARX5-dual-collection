#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "error: run as root" >&2
  exit 1
fi

state_dir=$(python3 -c 'from arx5_collection.collection.environment import ENVIRONMENT; print(ENVIRONMENT.paths.can_state_dir)')
state_file=${state_dir}/slcand.state

if [[ ! -s ${state_file} ]]; then
  echo "no managed CAN process state found: ${state_file}"
  exit 0
fi

while IFS='|' read -r label process_id tty_path interface_name; do
  if ip link show "${interface_name}" >/dev/null 2>&1; then
    ip link set "${interface_name}" down || true
  fi

  if [[ ${process_id} =~ ^[0-9]+$ ]] && [[ -r /proc/${process_id}/cmdline ]]; then
    process_command=$(tr '\0' ' ' </proc/"${process_id}"/cmdline)
    if [[ ${process_command} == *slcand* ]] && [[ ${process_command} == *"${tty_path}"* ]]; then
      kill "${process_id}"
      wait "${process_id}" 2>/dev/null || true
      echo "stopped ${label}: ${interface_name} (pid ${process_id})"
    else
      echo "warning: pid ${process_id} no longer matches managed slcand; not killed" >&2
    fi
  fi
done <"${state_file}"

rm -f "${state_file}"
