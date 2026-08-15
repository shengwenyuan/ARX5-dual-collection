#!/usr/bin/env bash
set -euo pipefail

parameter_path=/sys/module/usbcore/parameters/usbfs_memory_mb
required_mb=${ARX5_USBFS_MEMORY_MB:-256}

if [[ ! ${required_mb} =~ ^[0-9]+$ ]] || ((required_mb < 1)); then
  echo "error: ARX5_USBFS_MEMORY_MB must be a positive integer" >&2
  exit 1
fi
if [[ ! -r ${parameter_path} ]]; then
  echo "error: missing ${parameter_path}" >&2
  exit 1
fi

current_mb=$(<"${parameter_path}")
if ((current_mb < required_mb)); then
  printf '%s\n' "${required_mb}" >"${parameter_path}"
  current_mb=$(<"${parameter_path}")
fi
if ((current_mb < required_mb)); then
  echo "error: usbfs_memory_mb=${current_mb}, require at least ${required_mb}" >&2
  exit 1
fi

echo "usbfs_memory_mb=${current_mb} (required >= ${required_mb})"
