#!/usr/bin/env bash
set -euo pipefail

ROOT=/workspace/ARX5-dual-collection
CONFIG=${PI05_CONFIG:-$ROOT/config/pi05_arx5_joint_sft.toml}
MODE=${PI05_MODE:-train}

source "$ROOT/scripts/cloud/pi05_env.sh"
export PI05_PYTHON="$OPENPI_WORKSPACE/.venv/bin/python"
export PYTHONUNBUFFERED=1

[[ -r "$CONFIG" ]] || { echo "ERROR: missing config: $CONFIG" >&2; exit 1; }
[[ -x "$PI05_PYTHON" ]] || { echo "ERROR: missing Python: $PI05_PYTHON" >&2; exit 1; }
[[ "$MODE" == smoke || "$MODE" == train ]] || {
    echo "ERROR: PI05_MODE must be smoke or train" >&2; exit 1
}

GPU_COUNT=$($PI05_PYTHON -c 'import torch; print(torch.cuda.device_count())')
[[ "$GPU_COUNT" == 8 ]] || { echo "ERROR: expected 8 GPUs; found $GPU_COUNT" >&2; exit 1; }

if [[ "$MODE" == train ]]; then
    WANDB_FILE=/mnt/cfs/data/swy/personal/wandb
    [[ -r "$WANDB_FILE" ]] || { echo "ERROR: missing W&B credentials: $WANDB_FILE" >&2; exit 1; }
    export WANDB_ENTITY="$(sed -n '1{s/\r$//;p;}' "$WANDB_FILE")"
    export WANDB_API_KEY="$(sed -n '2{s/\r$//;p;}' "$WANDB_FILE")"
fi

exec "$PI05_PYTHON" "$ROOT/scripts/cloud/train_pi05_arx5.py" \
    --config "$CONFIG" --mode "$MODE"
