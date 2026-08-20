# DAgger 事件分类、独立导出与混合计划

- Status: `implemented and W3-verified`
- Updated: 2026-08-20
- Parent: `docs/dagger/requirements.md`、`docs/data-cleaning/pi05-mcap-to-lerobot.md`
- Classifier: `dagger-authority-v1`

## 目标与边界

DAgger 后处理位于通用清洗之后、模型 selector 之前。它不修改原始 MCAP、metadata、`quality.json` 或 `frame_index.jsonl`，只把稀疏 `/dagger/authority` 转换为可审计的控制区间。

首版先把 DAgger correction 单独导出为 LeRobot 并验证。验证通过后，在 selection 层与普通 demonstration 合并，再复用同一个 exporter 生成供 dataloader 使用的单一 LeRobot。混合不复制样本；权重只进入 provenance，尚不改变当前 dataloader 采样。

## 固定事件分类

| 半开区间 | 分类 | 训练资格 |
|---|---|---|
| Episode 开始或 `RESUME_REQUESTED` → `POLICY_ACTIVE` | `resume` | 无 |
| `POLICY_ACTIVE` → `TAKEOVER_REQUESTED` | `policy` | 无 |
| `TAKEOVER_REQUESTED` → `HUMAN_ACTIVE` | `handover` | 无 |
| `HUMAN_ACTIVE` → `RESUME_REQUESTED` | `expert_correction` | 有 |
| `FAULT_HOLD` → Episode 结束 | `fault` | 无 |

事件含义写死在 `dagger-authority-v1`，不允许 recipe 重映射。人工区间以 Episode 结束或 `FAULT_HOLD` 收尾但没有 `RESUME_REQUESTED` 时标记为 `incomplete_correction`，只审计。后续 fault 不否定此前已经闭合的 correction。

## 时间与一致性

1. authority sequence 必须连续，monotonic、bag time 和 control epoch 不得回退。
2. 每个事件与 metadata `control_segments` 的对应边界反推同一个 Episode monotonic anchor；离散超过 `1 µs` 时整条 authority timeline 无效。
3. 用事件 bag time 与语义 offset 估计 Episode bag anchor；发布延迟离散超过 `2 ms` 时无效。边界采用最小 anchor，避免用较晚接收时刻延长前一区间。
4. frame group 以 overview Color 的 `bag_timestamp_ns` 进入区间；区间内部仍按既有 Header 规则完成图像和双臂因果关联。
5. authority 与 metadata 任一不一致时输出 `valid=false`，不猜测、不训练。

## 产物

```text
audit/<episode_id>/
  quality.json
  frame_index.jsonl
  authority/
    quality.json
    segments.jsonl

derived-dagger/selection/
  sample_index.jsonl
  segments.jsonl
  selection.json
  source_manifest.jsonl
```

`source_manifest.jsonl` 保留 source Episode、split group、collection type、intervention ID、authority segment、bag-time 边界和预留 sample weight。LeRobot 导出后伴随 manifest 额外写入 `lerobot_episode_index`，可精确对应派生 episode。

## 处理顺序

```text
clean
  → classify-dagger
  → select-pi05-eef-dagger
  → to-lerobot（独立 correction 数据集）
  → validate-pi05 / validate-openpi
  → mix-selections（验证通过后）
  → to-lerobot（单一混合训练集）
```

DAgger selector 对每个完整 correction 独立运行原有 v2 等 EEF 距离配方；不跨 intervention 拼接，不增加 pre/post roll，不修改 idle、最短运动段、尾部裁剪、action 或 gripper 规则。`fail/aborted` 和 C 级 Episode 仍不进入训练。

## CLI 模板

```bash
arx5-dataset classify-dagger \
  --input-root <raw-dagger-root> \
  --audit-root <audit-root>

arx5-dataset select-pi05-eef-dagger \
  --input-root <raw-dagger-root> \
  --audit-root <audit-root> \
  --output-root <dagger-derived-root> \
  --task '<task>' \
  --left-gripper-open <value> --left-gripper-closed <value> \
  --right-gripper-open <value> --right-gripper-closed <value> \
  --gripper-tolerance <existing-recipe-value>

arx5-dataset mix-selections \
  --input demonstration=<demo-selection> \
  --input dagger=<dagger-selection> \
  --weight demonstration=1 \
  --weight dagger=1 \
  --output-root <mixed-derived-root>
```

`--weight` 当前只记录未来采样意图，`selection.json` 明确写 `weighting_applied=false`。在 dataloader 真正消费权重前，不宣称加权训练已经启用。

混合前必须精确复用 demonstration selection 的夹爪标定与 tolerance。W3 v2 当前为左开 `-2.7309837341`、右开 `-2.4361028671`、闭合均为 `0`、tolerance `0.001`；混合器会拒绝不同契约。

## 当前验收

- authority 正常、未闭合、fault、metadata 不一致和 Shadow 用例已覆盖。
- W3 候选发布镜像 `arx5-dual-collection:dataset-dagger-postprocess-20260820` 已在不挂载源码、断网条件下处理两条真实 DAgger Episode：B 级 Episode 得到 2 段完整 correction，A 级 Episode 得到 3 段；均 `valid=true`。
- A 级 Episode 共 13 个 authority 事件、3 次介入，bag anchor spread 为 `78,465 ns`。独立 selection 得到 3 个 segment、994 个训练有效样本；独立 LeRobot 为 3 个 episode / 994 frames，并通过 π0.5 与 OpenPI loader。
- 使用与 demonstration 完全一致的 v2 配方后，两条 DAgger Episode 的 5 段 correction 均进入完整版 selection。混合结果为 54 个 segment、29,769 个样本索引、27,816 个训练有效样本；来源为 49 条 demonstration Episode 与 2 条 DAgger Episode，不复制数据。
- selection 混合器已实测拒绝夹爪 tolerance 漂移，也会拒绝 filter、state/action、sampling contract 不一致和重复 sample/segment。
- task prompt 也是混合硬契约。完整版验收首次发现 demonstration=`Stacking paper cups`、DAgger=`stacking five paper cups` 漂移；修复后混合器会提前拒绝，最终数据统一使用 v2 实际 prompt `Stacking paper cups`。
- 完整混合 LeRobot 已发布到 W3 `/home/lenovo/swy/reports/derived/stacking_five_paper_cups_v2_dagger_mixed_20260820`：54 episodes、27,816 frames、162 个视频、54 个 Parquet，大小约 300 MB；5 个 DAgger correction 对应 LeRobot episode 49–53。
- 完整数据已通过 π0.5 loader 与固定 OpenPI transform：三路 `224×224×3` RGB、32 维 state、`50×32` action、69 个有效 prompt token。
- 本地全仓：`277 passed, 2 skipped`。

## 当前训练处置

本批 2 条 DAgger 源 Episode、5 段 correction 共提供 1,286 个训练有效样本，占混合数据约 `4.6%`。它足以用于短程 fine-tune、过拟合或 dataloader/训练链路 smoke test，但不足以支持模型改善或发布结论。

正式重训前必须再收集可按 source Episode 隔离的 DAgger batch，并留出从未进入训练的 correction Episode 做验证。切分单位始终是 source Episode，禁止把同一 Episode 的不同 intervention 分散到 train/validation。
