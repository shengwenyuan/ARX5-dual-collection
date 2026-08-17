# 数据质量与轻量清洗需求

- Status: `implemented-v1`
- Parent: `meta_plan.md`
- Downstream: `docs/data-cleaning/pi05-mcap-to-lerobot.md`
- Input: Episode `episode.mcap + metadata.json`
- Immediate target: 对一个路径下的 Episode 批次离线检查与轻量清洗，支持小批过拟合验证

## 现状与缺口

现有采集链路已经能够审计八路 Topic、消息类型、帧数、频率、最大间隔、Header 时间戳单调性和同机 RGB-D 配对，但尚未形成数据清洗契约：

- 没有定义八路共同可用的启末区间。
- 没有冻结重复、逆序、明显缺帧和孤立 RGB/Depth 的处理策略。
- 没有定义三颗独立 D405 的离线时间配组与容差。
- 没有定义 1 kHz 双臂状态如何关联到 30 Hz 视觉样本。
- 没有统一的 Episode `A / B / C` 质量档次和模型无关帧组索引。

`meta_plan.md` 禁止采集期跨相机等待、丢帧、重组、插值或伪同步。轻量清洗必须位于采集之后，不能反向改变 Source、Recorder 或原始 Episode 语义。

## 继承边界

- 原始 Episode 不可修改，正式目录继续严格只有 `episode.mcap + metadata.json`。
- 不重写原消息，不插值、补帧、重复、伪造或修改 Header 时间戳。
- “清洗”首先定义为验证、选择真实样本和生成派生索引，不等同于生成一份看似连续的新 MCAP。
- 所有舍弃都必须在报告中给出 Topic、原因、原始时间戳和数量；不得静默丢弃。
- 默认不生成 SHA。策略、工具和输出必须有明确 Schema/Policy 版本。
- 采集期在线监督继续负责发现停流；离线模块负责完整 MCAP 的确定性审计与训练可用性判断。
- 清洗命令只接受已存在的 Episode 路径，不启动、不连接也不监督采集 Session、CAN、ROS Source 或 Recorder。
- 质量判断只使用数据结构、时间戳和消息可用性，不读取任务未来结果来构造模型特征，不产生 oracle 信息。

## 建议产物

派生产物写入独立数据根，不进入原 Episode：

```text
cleaned/<batch_id>/<episode_id>/
  quality.json
  frame_index.jsonl
```

- `quality.json`：输入身份、Policy 版本、逐 Topic 统计、问题清单、有效时间范围、配对覆盖率和最终 `A / B / C` 档次。
- `frame_index.jsonl`：每行一个模型无关帧组，只引用真实消息的 Topic、Header 时间戳、序号及时间差；不定义 action/label，不复制图像或机械臂载荷。
- v0.1 不生成 cleaned MCAP；若模型加载器无法消费索引，再独立对齐导出格式。

`frame_index.jsonl` 是各训练链路共享的模型无关边界。π0.5 等下游可生成自己的 sample index、action、筛选片段和导出格式，但不得把模型、outcome 或 action-horizon 语义写回通用清洗产物。

## 建议流水线

```text
Discover
  -> Validate Episode/Schema/Topic/Type
  -> Audit per-stream Header timeline
  -> Pair same-camera RGB + aligned Depth
  -> Compute common usable interval
  -> Group three-camera real frames
  -> Associate real left/right arm samples
  -> Apply A/B/C quality policy
  -> Write quality.json + frame_index.jsonl
```

### 1. 输入验证

- 扫描输入路径下所有已原子提交的 Episode，以 Episode 为独立处理单位。
- 检查 MCAP 可读、八个必需 Topic 存在且类型固定、metadata 与 MCAP 身份一致。
- `.partial` 只报告，不修改。`success / aborted / fail` 均可生成数据质量报告；outcome 是否影响训练资格由下游决定，不写入帧组特征。

### 2. 单 Stream 时间线

逐 Topic 统计：

- 第一/最后 Header 时间戳、消息数、持续时长和频率。
- 相邻间隔分布、最大间隔、超过阈值的 gap 位置。
- 完全相同时间戳、逆序时间戳和无效时间戳。
- 相同时间戳对应相同或不同载荷的事实；不使用 SHA，可直接比较相邻序列化载荷。

### 3. 同机 RGB-D

- 同一颗 D405 的 Color 与 aligned Depth 只按完全相同 Header 时间戳配对。
- 单侧孤帧保留在原 MCAP，但不进入派生帧组；报告其位置和方向。
- 不以最近邻替代同机 RGB-D，不修改帧数，不隐藏录制启末边界孤帧。

### 4. 有效区间

建议先取所有必需 Stream 有效时间范围的交集：

```text
start = max(first_valid_stamp_per_required_stream)
end   = min(last_valid_stamp_per_required_stream)
```

派生索引只选择交集内的真实消息，原 MCAP 不裁剪。交集为空时记为 `C` 且无法生成帧组。首版不提供“最短有效时长”配置或判定。

### 5. 三相机离线配组

D405 不支持多机硬件同步，因此这里的“对齐”只能表达真实帧之间的时间邻近关系：

- 固定 overview RGB-D 为 30 Hz 帧组锚点。
- left/right 只选择 `±16.7 ms` 内最近的真实 RGB-D 对，并记录带符号时间差。
- 超出容差时该锚点样本不进入训练索引；不等待、不复制、不插值。
- 报告三相机配组覆盖率、时间差分布和连续失败区间。

### 6. 双臂状态关联

- 为避免对称最近邻引入未来状态，建议将一个帧组的 `observation_cutoff_ns` 定义为三颗相机所选真实帧时间戳的最大值。
- 建议为左右臂分别选择 `observation_cutoff_ns` 之前 `2 ms` 内最新的真实 ArmState，禁止选择 cutoff 之后的未来状态。
- 索引记录原始 ArmState Header 时间戳和相对 cutoff 的时间差。
- 不对关节、夹爪或 EEF 数值插值；超出容差时该帧组无效。

这一定义只形成可追溯观测帧组，不定义训练 action。任何未来 action、窗口、归一化统计或 train/validation 划分必须由独立训练数据层完成，并以 `observation_cutoff_ns` 为因果边界。

## A/B/C 质量档次

- 质量档次不删除原 Episode，也不等价于运行期 outcome。
- `A`：结构完整且时间质量达到严格门槛，可直接进入首选训练集合。
- `B`：存在可明确定位并从帧组索引排除的局部问题，仍保留足够真实有效帧组。
- `C`：结构异常、有效覆盖不足或无法生成帧组；保留原始数据与完整报告，不整体删除 Episode。
- 即使是 `C`，只要 MCAP 可读，仍应尽量输出有效子集索引和所有问题；不可读时只输出 `quality.json`。
- 档次只基于数据质量事实；任务成功、动作内容或 Episode 后续结果不得作为帧组选择条件，避免 oracle 和选择泄漏。

## 建议模块边界

```text
src/arx5_collection/cleaning/
  models.py       # Audit、Issue、Grade、FrameGroup 契约
  reader.py       # MCAP 顺序读取与最小消息引用
  timeline.py     # 单 Stream 时间线与 gap/duplicate 检查
  pairing.py      # 同机 RGB-D、三相机与双臂真实样本关联
  policy.py       # 阈值、Issue 与 A/B/C 分档
  store.py        # 独立派生产物原子提交
  cli.py          # 批量入口，仅解析参数和展示结果
```

清洗部署冻结为独立离线执行，不把批处理逻辑塞进长生命周期采集 Session。建议入口为 `arx5-dataset clean --input-root ... --output-root ...`，使用非 privileged 离线容器。采集与清洗可使用同一 Python package，但部署和进程所有权分离。

## 首版测试

- 构造正常、缺 Topic、错类型、空 MCAP、重复、逆序、内部 gap 和启末孤帧用例。
- 覆盖三相机容差边界、缺一机、双臂最近样本超限和有效区间为空。
- 同一输入与 Policy 必须生成完全一致的 grade 和索引。
- 任何失败不修改原 Episode；派生目录使用临时目录和原子提交。
- 使用已录制小批 Episode 回放，确认模型索引只包含真实可追溯样本。

## 已对齐决策

- 清洗与采集完全解耦，在统一录制完成后对指定路径下的 Episode 逐条离线执行。
- overview RGB-D 是三相机帧组锚点，left/right 使用 `±16.7 ms` 内最近的真实 RGB-D 对。
- 双臂关联容差为 `2 ms`，禁止数值插值，并必须额外满足无 leakage/oracle。
- 有效启末区间取八路共同交集；首版不讨论、不实现最短有效时长。
- Episode 使用 `A / B / C` 质量档次，不因局部问题简单整体 reject 或删除。
- 原始 MCAP 和 metadata 不修改；清洗输出是独立派生数据。

## v1 已冻结决策

1. 使用 `observation_cutoff`，双臂只取过去 `2 ms` 内最新 ArmState，禁止未来值和插值。
2. A 为覆盖率 `>=99%` 且无时间线 warning；B 为覆盖率 `>=95%` 或存在 gap warning；C 为 `<95%`、无有效帧组或结构异常。
3. 重复/逆序 Header 产生质量 issue 并降为 B；相机 `>67 ms`、机械臂 `>3 ms` gap 降为 B。无法满足配组或 arm age 的帧不进入索引。
4. 清洗层只输出模型无关 `quality.json + frame_index.jsonl`；action、task、success 和 50 Hz 筛选只存在于独立 π0.5 层。
5. 派生输出使用临时目录原子提交；目标已存在时失败，不覆盖。新策略或重跑必须使用新输出根或 dataset version。
