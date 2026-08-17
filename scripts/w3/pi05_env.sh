#!/usr/bin/env bash

export PI05_W3_ROOT="${PI05_W3_ROOT:-/home/lenovo/swy/pi05-runtime}"
export OPENPI_WORKSPACE="${PI05_W3_ROOT}/workspace/openpi"
export ARX5_WORKSPACE="${PI05_W3_ROOT}/workspace/ARX5-dual-collection"
export PI05_DATA_ROOT="${PI05_W3_ROOT}/data/pi05"

export OPENPI_DATA_HOME="${PI05_DATA_ROOT}/models"
export HF_HOME="${PI05_DATA_ROOT}/cache/huggingface"
export HF_HUB_CACHE="${HF_HOME}/hub"
export HF_LEROBOT_HOME="${PI05_DATA_ROOT}/datasets/lerobot"
export JAX_COMPILATION_CACHE_DIR="${PI05_DATA_ROOT}/cache/jax"

export PYTHONPATH="${ARX5_WORKSPACE}/src:${OPENPI_WORKSPACE}/src:${OPENPI_WORKSPACE}/packages/openpi-client/src${PYTHONPATH:+:${PYTHONPATH}}"
export PATH="${OPENPI_WORKSPACE}/.venv/bin:${PATH}"

export PI05_BASE_PARAMS="${PI05_DATA_ROOT}/models/openpi-assets/checkpoints/pi05_base/params"
export PI05_SFT_ROOT="${PI05_DATA_ROOT}/checkpoints/pi05_arx5_joint_sft/stacking_five_paper_cups_pi05_v1"
export PI05_SFT_CHECKPOINT="${PI05_SFT_ROOT}/9999"
