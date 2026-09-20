# checkpoint 统一配置接入

2026-09-20。本轮选用 003 的 `w5_0918_0919_slot4_old502_v1_20260920-0130/29999`，原推理配置中的 `model_variant=arx5_base`。没有修改模型、权重、训练源码或预处理实现，没有引入 tipcrop。

## 接入边界

**本能力由 DAgger（takeover/shadow）和 infer 共用。** `scripts/arx5` 将三个模式送入同一个启动函数，统一调用 `dagger.unified_config.prepare_policy`，复用同一个 policy-server 和 RTC runtime。保留既有 `dagger` 包位置，避免为 infer 复制配置或调度实现。

infer 自身仅负责自主采集、踏板 success/fail、动作事实记录与 episode 落盘接线；不拥有独立的模型配置、加载工厂或 scheduler。回归测试覆盖三个真实 CLI 参数入口，确认共用相同的配置生成和模型挂载路径，仅 infer 增加自己的采集命令 override。

`arx5 infer --inference-config …` 和 `arx5 dagger --inference-config …` 复用同一个转换入口：

1. 读取 checkpoint 对应的 SafeInfer TOML，检查 checkpoint 类型、完整目录 SHA、归一化资产和相机序列号。
2. 从这份 TOML 取得 checkpoint、repo_id、instruction、model_variant、OpenPI 源码目录、控制频率、chunk、执行窗口、flow steps、延迟估计和固定夹爪偏移。checkpoint 的 max delay 读取 `assets/policy_type.json`。
3. 使用既有 collection TOML 作为站点运行模板，生成 `$ARX5_OUTPUT_ROOT/.policy/policy.toml`。collector 和 policy-server 共用该文件。原 TOML、模板均不改写；生成配置记录来源路径和 SHA。
4. 生成 Compose override，把匹配的 OpenPI 源码只读挂载到模型容器 `/opt/checkpoint-openpi`。模型端在首次 import OpenPI 前选择该源码，再由 `model_variant` 选择配套模型和数据变换。`arx5_base` 使用该源码内的 `Pi05RtcConfig`、`create_data_config` 和已有 norm stats；200 token、discrete state 与该变体原有加载契约一致。
5. 将规范化后的运行参数交给现有 RTC scheduler；保留既有服务协议、踏板、HOME、停止保护、Recorder、MCAP 与 infer metadata 流程。

本版支持 `standard`、`arx5_base`；其他模型变体明确拒绝。开闭方向偏移不相同也拒绝，避免静默丢掉原配置语义。本次二者均为 0.02，可直接映射已有固定 offset。collection 的既有夹爪输出限幅仍由运行模板控制（当前 0–1），不复制 SafeInfer 的无范围约束行为。机器人话题、CAN、HOME、设备生命周期由 collection 的站点配置和 profile 管理，不从 SafeInfer 接管这些执行入口。

50 Hz 时 max delay=10，对应 200 ms 预算。转换层保留模板的 50 ms margin，将 policy wait 上限收至 150 ms，并将 Snapshot timeout 上限收至 100 ms；仍使用原配置校验和调度器。该静态预算不是实际闭环延迟验收结论。

## w5 启动指令

在 w5 终端执行。无需先运行 SafeInfer 的 service/start 脚本：

```bash
cd /home/lenovo/swy/ARX5-dual-collection
export ARX5_CHECKPOINT_ROOT=/home/lenovo/swy/pi05-runtime/data/pi05/checkpoints/pi05_arx5_joint_train_rtc
export ARX5_OUTPUT_ROOT=/home/lenovo/swy/reports/2026-09-20/slot4-infer-acceptance
export ARX5_TASK_DESCRIPTION='place the screwdriver bit into organizer slot 4'

arx5 infer --rgb-only \
  --runtime-config /var/lib/arx5-collection/infer-policy.toml \
  --inference-config /home/lenovo/zhoushihao/pi05_jax_safeinfer/config/experiments/w5-0918-0919-slot4-old502-30k/inference.toml
```

`--runtime-config` 提供 collection 的站点运行模板；checkpoint、prompt、50 Hz 等模型运行字段被统一配置覆盖。无需手动复制 checkpoint 路径到第二份 TOML。首次准备会读取完整 checkpoint 计算 SHA，随后服务启动再验证 SHA 并 warm-up。

相机选择仍来自 station，`--rgb-only` 记录三路 RGB 与双臂状态；去掉该选项则使用已有完整任务配置。reports 路径和落盘格式不变。

READY 右踏板 → HOME → RUNNING；运行中右 success／左 fail；落盘后等待新右踏板。连续三局、长按/释放、Ctrl+C 和实际 MCAP 按 [真机验收文档](w5-infer-acceptance.md) 验证。

## 离线验证与部署

- 本地全量测试：407 passed、1 skipped（原有 OpenPI client 环境跳过），包括 13 项新增配置与入口回归测试。
- w5 的真实 checkpoint 在原有 policy 镜像依赖中成功恢复为匹配源码的 `openpi.experiments.arx5_base.model.Pi05ActionPrefixModel`，无前缀和有前缀 warm-up 输出检查通过；隔离容器 `--network none`，没有启动模型监听服务、CAN、相机或机械臂。
- policy 镜像仅覆盖 collection 的加载代码，使用 `Dockerfile.policy-update`、`--network=none --pull=false`，不下载组件。collector 镜像继续复用。
- 真机闭环时延、踏板行为和落盘仍待用户验收，不因离线模型输出检查通过而提前锁定完成版本。
