# π0.5 八卡云端训练环境

- Status: `full-training-complete`
- openpi commit: `15a9616a00943ada6c20a0f158e3adb39df2ccac`
- Host: `root@10.30.2.123:7498`
- Training target: `pi05_base`、ARX5 双臂 joint-only SFT

## 目录边界

只使用以下两棵目录：

```text
/workspace/openpi                         # 固定版本官方代码与 .venv
/workspace/ARX5-dual-collection          # 本项目当前 main 工作树
/mnt/cfs/data/swy/pi05/models             # openpi 基座模型与资产
/mnt/cfs/data/swy/pi05/checkpoints        # SFT checkpoints
/mnt/cfs/data/swy/pi05/datasets           # LeRobot 数据集
/mnt/cfs/data/swy/pi05/cache              # uv/Hugging Face/JAX cache
/mnt/cfs/data/swy/pi05/logs               # 训练日志
```

当前资产根固定为 `/mnt/cfs/data/swy`；PFS 迁移完成后再整体切换。该根之外的 `/mnt` 路径禁止写入。

## 固定环境

官方仓库以 detached HEAD 固定到权威基线，依赖使用其 `uv.lock` 安装。主机侧 Ubuntu/CUDA 驱动不写入项目环境；训练 Python 与 wheel 依赖位于 `/workspace/openpi/.venv`，大缓存通过环境变量进入 CFS。

固定依赖中的 NCCL 2.26.2 在容器/VM 中可能因默认 cuMem host allocation 与 NUMA 能力不匹配而卡在 communicator 初始化。环境文件按 [NVIDIA NCCL 官方排障建议](https://docs.nvidia.com/deeplearning/nccl/archives/nccl_2265/user-guide/docs/troubleshooting.html#cumem-host-allocations) 设置 `NCCL_CUMEM_HOST_ENABLE=0`，使用本机 400 GiB `/dev/shm` 回退。

RPBZZZ6 上的实际 8-way FSDP 还会在 NCCL 2.26.2 的 `ncclGroupEnd()` 触发 CUDA illegal address；单卡相同模型可完整训练，确认问题在 collective。按 [NVIDIA NCCL 2.26.5 release notes](https://docs.nvidia.com/deeplearning/nccl/release-notes/rel_2-26-5.html) 采用同 minor 的 2.26.5 补丁 wheel 后，8 卡单步通过。该项是唯一偏离 `uv.lock` 的主机兼容覆盖，安装脚本在每次 `uv sync` 后显式重放并由验证脚本检查版本。

```bash
source /workspace/ARX5-dual-collection/scripts/cloud/pi05_env.sh
/workspace/ARX5-dual-collection/scripts/cloud/install_pi05_env.sh
```

每次训练或验证前必须 source 同一环境文件，避免模型、LeRobot 数据和缓存落到 root home 或其他 `/mnt` 目录。

## 验证门槛

```bash
source /workspace/ARX5-dual-collection/scripts/cloud/pi05_env.sh
python /workspace/ARX5-dual-collection/scripts/cloud/verify_pi05_host.py
```

训练就绪必须同时满足：

1. JAX 识别 8 张 GPU，并完成每卡矩阵计算和跨 8 卡 collective。
2. 固定版本 openpi 能导入，ARX5 配置保持 32 维模型 action、50-step horizon、8-way FSDP。
3. `pi05_base` 参数能从 CFS 完整加载到模型。
4. 最终 LeRobot 真实 batch 完成 loss/gradient/update 和 5-step 自动入口 smoke。
5. 正式训练前必须对最终 LeRobot 数据集重新计算 fresh norm stats，禁止复用临时测试数据统计。

模型与优化器验证复用官方 `scripts/train.py` 的 `init_train_state`、`train_step` 和 FSDP sharding：

```bash
source /workspace/ARX5-dual-collection/scripts/cloud/pi05_env.sh
python /workspace/ARX5-dual-collection/scripts/cloud/smoke_pi05_train_step.py
```

## 与数据链路的边界

云端环境只消费 `pi05_dataset` 导出的最终 LeRobot 数据集和训练配置，不读取 MCAP，也不依赖采集 Session、Recorder、ROS 2、CAN 或相机运行时。MCAP 审计与转换仍是独立离线流水线；更换训练机器不得改变清洗结果、sample index 或数据集内容。

最终 LeRobot 数据已在 w3 对齐并通过 openpi loader 验证。云端执行顺序为：上传/挂载 `stacking_five_paper_cups_pi05_v1`、核验 manifest、计算 fresh norm stats、真实 batch loader smoke、单步训练、再启动完整 SFT。

正式数据对齐后使用项目侧入口，不改 openpi 的全局 config registry：

```bash
source /workspace/ARX5-dual-collection/scripts/cloud/pi05_env.sh

python /workspace/ARX5-dual-collection/scripts/cloud/compute_pi05_norm_stats.py \
  --repo-id <lerobot_repo_id>

PI05_MODE=smoke /workspace/ARX5-dual-collection/scripts/cloud/run_pi05_8gpu.sh
```

全量训练参数集中在 `config/pi05_arx5_joint_sft.toml`，入口脚本不承载超参数。`PI05_MODE=train` 启动 10,000 steps 完整任务，`PI05_MODE=smoke` 应用配置中的 smoke overrides、禁用 W&B 和 checkpoint 保存。checkpoint、fresh stats 和 JAX compilation cache 均写到 `/mnt/cfs/data/swy/pi05`。

## 当前验收结果

- JAX 0.5.3：8/8 GPU 可见，矩阵计算和跨卡 collective 通过。
- Torch 2.7.1+cu126：CUDA 可用。
- `pi05_base/params`：已在 CFS 完整验证。
- 单卡 full model synthetic train step：通过，`loss=1.2532`。
- 8 卡、8-way FSDP、global batch 8 synthetic train step：通过，`loss=1.8881`、`grad_norm=7.8159`，约 28.4 秒。
- 完整 50-episode 数据 fresh stats：583/583 batch 通过，已写入 CFS 正式资产目录。
- 29 行自动入口 8 卡 smoke：5/5 steps 通过，loss 为 `0.0820→0.0762→0.0616→0.0736→0.0743`；W&B 关闭，未保存 checkpoint。
- 同一入口的 10,000-step 全量训练已完成；W&B run `tkoiompr` 的 loss 从约 `0.082` 收敛到 `0.00209`，最终 zero-based checkpoint 为 `9999`。
- 最终 SFT checkpoint 已用 6 个真实录制样本完成离线 policy 推理：输出均为 finite `(50, 14)`，首步 joint 最大跳变量约 `0.00579 rad`。
- 每卡峰值预分配约 73.5 GiB，低于单卡约 96 GiB。

因此当前 CFS 主机已完成 10,000-step π0.5 SFT，并保留 `5000` 与 `9999` 两组 checkpoint；w3 部署默认消费最终 `9999` 权重。
