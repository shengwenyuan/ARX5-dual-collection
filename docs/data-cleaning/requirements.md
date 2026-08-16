# 数据质量与轻量清洗需求

- Status: `draft-alignment`
- Parent: `meta_plan.md`
- Input: Episode `episode.mcap + metadata.json`
- Immediate target: 小批 Episode 过拟合验证

## 现状与缺口

现有采集链路已经能够审计八路 Topic、消息类型、帧数、频率、最大间隔、Header 时间戳单调性和同机 RGB-D 配对，但尚未形成数据清洗契约：

- 没有定义八路共同可用的启末区间。
- 没有冻结重复、逆序、明显缺帧和孤立 RGB/Depth 的处理策略。
- 没有定义三颗独立 D405 的离线时间配组与容差。
- 没有定义 1 kHz 双臂状态如何关联到 30 Hz 视觉样本。
- 没有统一的 Episode `pass / warning / reject` 结论和模型样本索引。

`meta_plan.md` 禁止采集期跨相机等待、丢帧、重组、插值或伪同步。轻量清洗必须位于采集之后，不能反向改变 Source、Recorder 或原始 Episode 语义。

## 继承边界

- 原始 Episode 不可修改，正式目录继续严格只有 `episode.mcap + metadata.json`。
- 不重写原消息，不插值、补帧、重复、伪造或修改 Header 时间戳。
- “清洗”首先定义为验证、选择真实样本和生成派生索引，不等同于生成一份看似连续的新 MCAP。
- 所有舍弃都必须在报告中给出 Topic、原因、原始时间戳和数量；不得静默丢弃。
- 默认不生成 SHA。策略、工具和输出必须有明确 Schema/Policy 版本。
- 采集期在线监督继续负责发现停流；离线模块负责完整 MCAP 的确定性审计与训练可用性判断。

## 建议产物

派生产物写入独立数据根，不进入原 Episode：

```text
cleaned/<batch_id>/<episode_id>/
  quality.json
  sample_index.jsonl
```

- `quality.json`：输入身份、Policy 版本、逐 Topic 统计、问题清单、有效时间范围、配对覆盖率和最终 verdict。
- `sample_index.jsonl`：每行一个模型样本，只引用真实消息的 Topic、Header 时间戳、序号及时间差；不复制图像或机械臂载荷。
- v0.1 不生成 cleaned MCAP；若模型加载器无法消费索引，再独立对齐导出格式。

## 建议流水线

```text
Discover
  -> Validate Episode/Schema/Topic/Type
  -> Audit per-stream Header timeline
  -> Pair same-camera RGB + aligned Depth
  -> Compute common usable interval
  -> Group three-camera real frames
  -> Associate real left/right arm samples
  -> Apply quality policy
  -> Write quality.json + sample_index.jsonl
```

### 1. 输入验证

- 默认只处理已原子提交且 outcome 为 `success` 的 Episode。
- 检查 MCAP 可读、八个必需 Topic 存在且类型固定、metadata 与 MCAP 身份一致。
- `aborted`、`fail`、`.partial` 默认报告并跳过，不进入训练索引。

### 2. 单 Stream 时间线

逐 Topic 统计：

- 第一/最后 Header 时间戳、消息数、持续时长和频率。
- 相邻间隔分布、最大间隔、超过阈值的 gap 位置。
- 完全相同时间戳、逆序时间戳和无效时间戳。
- 相同时间戳对应相同或不同载荷的事实；不使用 SHA，可直接比较相邻序列化载荷。

### 3. 同机 RGB-D

- 同一颗 D405 的 Color 与 aligned Depth 只按完全相同 Header 时间戳配对。
- 单侧孤帧保留在原 MCAP，但不进入派生模型样本；报告其位置和方向。
- 不以最近邻替代同机 RGB-D，不修改帧数，不隐藏录制启末边界孤帧。

### 4. 有效区间

建议先取所有必需 Stream 有效时间范围的交集：

```text
start = max(first_valid_stamp_per_required_stream)
end   = min(last_valid_stamp_per_required_stream)
```

派生索引只选择交集内的真实消息，原 MCAP 不裁剪。若交集为空或过短，Episode 直接 `reject`。

### 5. 三相机离线配组

D405 不支持多机硬件同步，因此这里的“对齐”只能表达真实帧之间的时间邻近关系：

- 选择一个 30 Hz 相机 Stream 作为样本锚点。
- 其他两颗相机只选择容差内最近的真实 RGB-D 对，并记录带符号时间差。
- 超出容差时该锚点样本不进入训练索引；不等待、不复制、不插值。
- 报告三相机配组覆盖率、时间差分布和连续失败区间。

### 6. 双臂状态关联

- 每个视觉样本为左右臂分别选择时间上最近的真实 ArmState。
- 索引记录原始 ArmState Header 时间戳和相对视觉锚点的时间差。
- 不对关节、夹爪或 EEF 数值插值；超出容差时该模型样本无效。

## 建议模块边界

```text
src/arx5_collection/cleaning/
  models.py       # Audit、Issue、Verdict、SampleIndex 契约
  reader.py       # MCAP 顺序读取与最小消息引用
  timeline.py     # 单 Stream 时间线与 gap/duplicate 检查
  pairing.py      # 同机 RGB-D、三相机与双臂真实样本关联
  policy.py       # 阈值与 pass/warning/reject
  store.py        # 独立派生产物原子提交
  cli.py          # 批量入口，仅解析参数和展示结果
```

建议使用独立 `arx5-dataset clean` 入口和非 privileged 离线容器，不把批处理逻辑塞进长生命周期采集 Session。采集与清洗可使用同一 Python package，但部署和进程所有权分离。

## 首版测试

- 构造正常、缺 Topic、错类型、空 MCAP、重复、逆序、内部 gap 和启末孤帧用例。
- 覆盖三相机容差边界、缺一机、双臂最近样本超限和有效区间为空。
- 同一输入与 Policy 必须生成完全一致的 verdict 和索引。
- 任何失败不修改原 Episode；派生目录使用临时目录和原子提交。
- 使用已录制小批 Episode 回放，确认模型索引只包含真实可追溯样本。

## 待对齐决策

1. 模型首版消费 `sample_index.jsonl + 原 MCAP`，还是必须生成重写后的训练文件。
2. 三相机锚点选择 overview、left，还是由任务配置指定。
3. 三相机最近真实帧的容差，以及覆盖率对应 warning/reject 的阈值。
4. 双臂最近真实状态的容差；是否允许最近邻选择但禁止数值插值。
5. 重复、逆序、明显 gap 和边界孤帧分别是样本级剔除、Episode warning 还是 Episode reject。
6. 有效区间是否采用八路交集，以及最短可用时长。
7. `aborted/fail` 是否永远排除，或允许显式诊断模式生成报告但不生成训练索引。

