#!/usr/bin/env bash

# Source this file on the π0.5 training host. Code stays on the local overlay;
# model, dataset, cache, checkpoint, and log payloads stay in the user CFS path.
export OPENPI_WORKSPACE="/workspace/openpi"
export ARX5_WORKSPACE="/workspace/ARX5-dual-collection"
export PI05_DATA_ROOT="/mnt/cfs/data/swy/pi05"

export OPENPI_DATA_HOME="${PI05_DATA_ROOT}/models"
export HF_HOME="${PI05_DATA_ROOT}/cache/huggingface"
export HF_HUB_CACHE="${HF_HOME}/hub"
export HF_LEROBOT_HOME="${PI05_DATA_ROOT}/datasets/lerobot"
export UV_CACHE_DIR="${PI05_DATA_ROOT}/cache/uv"
export UV_LINK_MODE="copy"
export JAX_COMPILATION_CACHE_DIR="${PI05_DATA_ROOT}/cache/jax"

# openpi pins NCCL 2.26.2. In container/VM environments its default cuMem host
# allocation can hang communicator setup when NUMA capability is unavailable.
# /dev/shm on this host is 400 GiB, so use NCCL's documented legacy fallback.
export NCCL_CUMEM_HOST_ENABLE="0"

export PYTHONPATH="${ARX5_WORKSPACE}/src:${OPENPI_WORKSPACE}/src:${OPENPI_WORKSPACE}/packages/openpi-client/src${PYTHONPATH:+:${PYTHONPATH}}"
export PATH="${OPENPI_WORKSPACE}/.venv/bin:${PATH}"
