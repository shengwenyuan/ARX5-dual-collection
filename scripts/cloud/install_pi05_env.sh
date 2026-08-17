#!/usr/bin/env bash
set -euo pipefail

source /workspace/ARX5-dual-collection/scripts/cloud/pi05_env.sh

expected_openpi_commit="15a9616a00943ada6c20a0f158e3adb39df2ccac"
actual_openpi_commit="$(git -C "${OPENPI_WORKSPACE}" rev-parse HEAD)"
if [[ "${actual_openpi_commit}" != "${expected_openpi_commit}" ]]; then
  echo "openpi commit mismatch: ${actual_openpi_commit}" >&2
  exit 1
fi

mkdir -p \
  "${PI05_DATA_ROOT}/models" \
  "${PI05_DATA_ROOT}/checkpoints" \
  "${PI05_DATA_ROOT}/datasets/lerobot" \
  "${PI05_DATA_ROOT}/cache/uv" \
  "${PI05_DATA_ROOT}/cache/huggingface" \
  "${PI05_DATA_ROOT}/cache/jax" \
  "${PI05_DATA_ROOT}/logs"

cd "${OPENPI_WORKSPACE}"
GIT_LFS_SKIP_SMUDGE=1 uv sync --frozen --python 3.11.15
GIT_LFS_SKIP_SMUDGE=1 uv pip install -e .

# openpi's lock pins 2.26.2. RPBZZZ6 8-way FSDP fails inside ncclGroupEnd
# with that build; NVIDIA's 2.26.5 patch release passes the full train-step smoke.
uv pip install \
  --python "${OPENPI_WORKSPACE}/.venv/bin/python" \
  --no-deps \
  nvidia-nccl-cu12==2.26.5

