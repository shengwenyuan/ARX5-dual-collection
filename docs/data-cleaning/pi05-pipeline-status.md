# π0.5 数据清洗与转换状态表

- Last updated: `2026-08-20`
- Current dataset version: `stacking_five_paper_cups_pi05_v1`
- Cleaning policy: `arx5-cleaning-v1`
- Selection policy: `pi05-arx-filter-v1`
- Time basis: `header_stamp_ns`
- Status: `v1 implemented, exported, OpenPI-verified and trained`
- Next selector: `pi05-arx-filter-v2-equal-eef-distance`（已实现并通过单测，正式数据验收 pending）
- Detailed design: `docs/data-cleaning/pi05-mcap-to-lerobot.md`
- DAgger postprocess: `docs/data-cleaning/dagger-postprocess.md`

## 维护约定

本文件是当前数据链路的长期状态表。后续修改清洗规则、阈值、字段、数据集版本或训练验收结果时，必须同步更新：

1. 页首日期、dataset/policy version 和总状态。
2. “功能状态表”中的状态、参数与实现入口。
3. “当前固定参数”和“正式数据结果”。
4. 若规则变化会改变样本选择，必须使用新的 dataset/filter version，不覆盖已有版本。
5. 数据 Artifact 契约、selection policy 与 OpenPI 模型契约必须分开描述。

状态取值统一使用：

- `enabled`：已实现，并在正式链路中启用。
- `verified`：已用正式数据或固定 OpenPI 环境验收。
- `disabled`：明确不进入当前版本。
- `pending`：代码或验收尚未完成，不能宣称 training-ready。

## 当前处理顺序

```text
已提交 Episode
  -> MCAP/metadata/Topic 校验
  -> Header 时间线审计
  -> RGB-D 与三相机帧组构造
  -> 双臂状态关联
  -> quality.json + frame_index.jsonl
  -> DAgger authority 分类（仅 collection_type=dagger）
  -> success/质量/长度筛选
  -> Header-based 50 Hz sample 构造
  -> 14 维 joint state/action 构造
  -> idle、短运动段和片段尾部裁剪
  -> sample_index.jsonl + segments.jsonl
  -> 三路 RGB LeRobot 导出
  -> LeRobot/OpenPI loader 验证
  -> fresh normalization stats
  -> pi05_base joint-only SFT
```

## 下一版：等 EEF 位移采样（implemented，正式数据验收 pending）

下一版不再用固定 `delta t` 构造 50 Hz 采样点，而是把轨迹重采样为近似等 EEF 位移的离散序列。该变化必须生成新的 filter/dataset version，不覆盖 v1。

已冻结的需求如下：

1. 当前 1000 Hz 数据仍是双臂实测位置代理，不引入独立 command/action Topic。
2. 直接读取双臂 ArmState Topic 已录制的 `eef_xyzrpy[:3]`，不通过 joint FK 重建 EEF；平移单位按 metre 解释。
3. EEF 只用于决定采样点；最终 `state/action` 继续使用 14 维 joint 表达，保持 joint-only SFT。
4. 按 `header_stamp_ns` 遍历真实状态样本。保留首点后，当
   `max(left_eef_delta, right_eef_delta) >= 5 mm` 时保留第一个越过阈值的真实样本。
5. `eef_delta` 使用上一个保留点与当前点的平移欧氏距离。沿 1000 Hz 轨迹累计路径长度理论上更精确，但当前选择端点距离以降低计算成本；不插值或生成虚构状态。
6. 夹爪归一化值变化 `>= 0.02` 时独立触发采样；最大采样间隔可配置，当前默认 `100 ms`。
7. 每个采样 tick 必须关联完整三相机帧组：选择满足
   `observation_cutoff_ns <= tick_ns` 的最新帧组，不允许三相机分别独立取帧，也不允许未来图像。
8. 继续使用 `header_stamp_ns` 做状态、EEF、RGB/Depth 和三相机的物理时间关联；`bag_timestamp_ns` 只留档和校验来源。
9. 不修改 OpenPI dataloader。导出的序列定义为“等距离轨迹索引”；LeRobot `fps` 是兼容性名义值，50-step horizon 表示约 50 个轨迹步，不再表示约 1 秒真实时间。
10. 保留每个样本的真实 `source_header_stamp_ns` 和相邻真实 `delta_time_ns`，避免名义 fps 混淆物理时间。

行为保持型结构重构已经完成：集中 Artifact TypedDict 与 `MessageRef` codec；拆分 selection pipeline 和 artifact codec；集中 OpenPI 模型契约；CLI 使用显式 handler；统一目录发布的原子可见语义。

当前实现入口为 `pi05_dataset.eef_selection.build_equal_eef_samples` 和 CLI `select-pi05-eef`。v1 的固定 50 Hz `select-pi05` 保留，不改变已有复现路径。

## 功能状态表

| 层级 | 功能 | 状态 | 当前规则/输出 | 主要实现入口 | 规则来源 |
|---|---|---:|---|---|---|
| 输入发现 | 发现已原子提交的 Episode | enabled | 必须同时存在 `episode.mcap` 与 `metadata.json`；Episode ID 不允许重复 | `pi05_dataset.discovery.discover_episode_dirs` | 项目数据契约 |
| DAgger 分类 | authority 与 metadata 双记录校验 | verified | 固定五类半开区间；只有完整 expert correction 可训练 | `dagger_dataset.classifier.classify_authority` | `dagger-authority-v1` |
| DAgger 选择 | correction 独立运行 v2 selector | verified | 无 pre/post roll；不跨 intervention；保留 source manifest | `dagger_dataset.selection.select_equal_eef_dagger_dataset` | DAgger 数据契约 |
| 数据混合 | demonstration + correction selection | verified | 不复制样本；权重只记录、尚未应用 | `pi05_dataset.mixing.mix_selections` | 当前首版决策 |
| MCAP Reader | 必需流和消息类型校验 | enabled | 三相机 RGB/Depth 六路 + 双臂状态两路，共八路 | `cleaning.reader.read_episode_scan` | 项目适配 |
| MCAP Reader | Header 与 bag time 留档 | enabled | 选择逻辑使用 `header_stamp_ns`；`bag_timestamp_ns` 仅用于来源引用和一致性校验 | `cleaning.models.MessageRef` | 当前 v1 决策 |
| 数值检查 | ArmState 有限值和维度检查 | enabled | 六关节；含 NaN/Inf 的状态不进入可用 ArmSample | `cleaning.reader.read_episode_scan`、`cleaning.models.ArmSample` | 项目质量门槛 |
| 时间审计 | 单流 Header 时间线统计 | enabled | count、首末时间、最大正 gap、重复和逆序 | `cleaning.timeline.audit_timeline` | 项目质量门槛 |
| 相机配对 | 同机 RGB/Depth 配对 | enabled | `header_stamp_ns` 必须完全相同；不插值、不补帧 | `cleaning.pairing._pair_same_camera` | 项目适配 |
| 相机配组 | 三相机完整帧组 | enabled | overview 为锚点；左右腕相机最近帧；容差 `±16.7 ms` | `cleaning.pairing._nearest_pair`、`build_frame_groups` | 项目适配 |
| 状态配组 | observation 对应双臂状态 | enabled | 选择 Header cutoff 前最新状态；最大 age `2 ms` | `cleaning.pairing._latest_arm` | 项目适配 |
| 质量报告 | A/B/C 分级 | enabled | A：coverage≥99%；B：coverage≥95%；否则 C；时间异常可降为 B | `cleaning.pipeline.inspect_episode` | 项目质量策略 |
| 通用索引 | 清洗产物原子写入 | enabled | 每个源 Episode 输出 `quality.json + frame_index.jsonl`；不修改 MCAP | `cleaning.store.write_cleaning_artifacts` | 项目数据契约 |
| 训练资格 | outcome 和质量筛选 | enabled | 只训练 `success`；排除 C；A/B 可进入 | `pi05_dataset.selection_pipeline.select_dataset` | OpenPI/DROID 方向 + 项目门槛 |
| 训练资格 | Episode 长度限制 | enabled | 最大 `180 s` | `Pi05Policy.max_episode_duration_s` | 项目参数 |
| 50 Hz sample | Header-based 因果采样 | enabled | 每 20 ms 选择 `observation_cutoff_ns <= tick` 的最新帧组 | `pi05_dataset.selection.build_samples` | π0.5 频率适配 + 项目时序策略 |
| 50 Hz sample | 图像 age 限制 | enabled | 最大 `40 ms`；约 30 Hz 图像可被相邻 50 Hz tick 复用 | `Pi05Policy.image_max_age_ns` | 项目参数 |
| 50 Hz sample | 双臂状态选择 | enabled | tick 前最新状态；最大 Header age `2 ms`；不插值 | `pi05_dataset.selection._latest_arm` | 项目参数 |
| 等 EEF 位移 sample | 双臂空间触发 | pending | `max(left,right) >= 5 mm`；端点欧氏距离；真实 Header tick | `pi05_dataset.eef_selection.build_equal_eef_samples` | 已实现、待正式数据验收 |
| 等 EEF 位移 sample | 夹爪/时间触发 | pending | 归一化夹爪变化 `>=0.02`；最大间隔 `100 ms` | `EqualEefPolicy` | 已实现、待正式数据验收 |
| 等 EEF 位移 sample | 完整帧组反向关联 | pending | 最新 `observation_cutoff_ns <= tick_ns`；image age `<=40 ms` | `eef_selection._latest_frame_group` | 已实现、待正式数据验收 |
| 状态构造 | 14 维 joint state | enabled | `[left J1..J6, left gripper, right J1..J6, right gripper]` | `pi05_dataset.actions.make_state` | OpenPI ARX/Aloha 适配 |
| 夹爪构造 | raw 到 `[0,1]` | enabled | `0=open, 1=closed`；超标定容差时报错 | `pi05_dataset.actions.GripperCalibration` | OpenPI 表达 + 项目标定 |
| 动作构造 | measured-position proxy | enabled | 当前无 command Topic，使用 `action[t] = state[t]` | `pi05_dataset.selection.build_samples` | 项目示教适配 |
| Idle 清洗 | 长 idle 删除 | enabled | 相邻 14 维 action 每维变化均 `<1e-3` 视为 idle；连续至少 24 帧删除 | `pi05_dataset.selection.select_nonidle_segments` | OpenPI DROID 时间尺度适配 |
| 片段筛选 | 短运动和尾部裁剪 | enabled | 运动段至少 54 帧；每段末尾裁 34 帧 | `select_nonidle_segments` | OpenPI DROID 时间尺度适配 |
| 片段索引 | 来源与训练资格记录 | enabled | 输出 `sample_index.jsonl`、`segments.jsonl`、`selection.json` | `pi05_dataset.artifact_codec.write_selection_artifacts` | 项目可追溯契约 |
| RGB 读取 | RGB8 + 历史 YUYV | enabled | 新 RGB8 原样读取；历史 YUYV 以 BT.601 limited-range 解码；导出 `640x360` RGB | `pi05_dataset.images.decode_color_message`、`extract_selected_rgb` | 项目相机适配 |
| LeRobot | 三相机 video dataset | verified | `cam_high`、左右 wrist、14 维 state/action、task、50 Hz | `pi05_dataset.openpi_contract`、`exporter.export_lerobot` | OpenPI/LeRobot 字段契约 |
| LeRobot | segment 到 episode | verified | 每个连续有效 segment 单独调用一次 `save_episode()` | `pi05_dataset.exporter.export_lerobot` | 保持时间连续性 |
| OpenPI adapter | 数据字段 repack | verified | 三路图像、state、actions、task prompt 映射到 OpenPI | `pi05_dataset.openpi_adapter.make_arx5_data_config` | OpenPI 官方 DataConfig |
| OpenPI transform | π0.5 shape 和 action chunk | verified | 图像 `224x224x3`；state padding 到 32；action chunk `50x32` | `pi05_dataset.validate.validate_openpi` | OpenPI 官方 transform |
| OpenPI action | joint delta + absolute gripper | verified | 12 个 joint 使用 delta；两夹爪保持 absolute | `LeRobotAlohaDataConfig` | OpenPI Aloha 配方 |
| Normalization | fresh norm stats | verified | 使用正式数据和 OpenPI transform 计算 q01/q99/std | `scripts/cloud/compute_pi05_norm_stats.py` | OpenPI 官方统计流程 |
| 训练交接 | pi05_base joint-only SFT | verified | 32 维模型 action、50-step horizon、8-way FSDP | `pi05_dataset.openpi_adapter.make_arx5_train_config` | OpenPI π0.5 配置 |

## 契约与模块边界

| 边界 | 内容 | 实现 |
|---|---|---|
| 数据 Artifact 契约 | JSON/JSONL 字段、`MessageRef` codec、TypedDict、schema version | `arx5_collection.artifacts`、`pi05_dataset.artifact_codec`、`schemas/` |
| Selection pipeline | success/质量筛选、采样、状态构造、idle 和 segment | `pi05_dataset.selection`、`selection_pipeline` |
| OpenPI 模型契约 | 相机 key、joint 顺序、14→32 维、名义 fps、action horizon、固定依赖版本 | `pi05_dataset.openpi_contract`、`openpi_adapter` |

`manifest.py` 仅保留兼容导出，不再包含 pipeline 或 codec 实现。CLI 子命令通过 `set_defaults(handler=...)` 显式分发，`main()` 不包含业务分支。

### 原子落盘语义

目录型产物统一使用 `atomic.staged_directory`：在目标同级临时目录完成写入，再以一次 `os.replace` 发布；目标已存在时拒绝覆盖，异常时清理 staging。该保证是同一文件系统、单写者前提下的“原子可见”，不承诺 `fsync` 掉电持久性或多写者协调。

cleaning、selection、LeRobot dataset 和 norm stats 的目录发布语义已经一致。LeRobot dataset 与其外置 `reports/conversion.json` 是两个目标，不能构成单次文件系统事务；dataset 目录是主产物，conversion report 仍是发布后的伴随报告。

## 当前固定参数

| 参数 | 当前值 |
|---|---:|
| 时间选择基准 | `header_stamp_ns` |
| LeRobot fps | `50` |
| action horizon | `50` |
| 跨相机容差 | `16.7 ms` |
| image max age | `40 ms` |
| arm max age | `2 ms` |
| 最大 Episode 时长 | `180 s` |
| idle delta threshold | `1e-3` |
| 最短连续 idle | `24 frames` |
| 最短有效运动 | `54 frames` |
| 每段尾部裁剪 | `34 frames` |
| 左夹爪 open / closed | `-2.7309837341 / 0` |
| 右夹爪 open / closed | `-2.4361028671 / 0` |
| task | `Stacking paper cups` |
| RGB 导出尺寸 | `640x360` |
| 模型 action dimension | `32` |

下一版 selector 默认值：EEF 位移 `5 mm`、夹爪归一化变化 `0.02`、最大采样间隔 `100 ms`、image max age `40 ms`、arm max age `2 ms`；LeRobot 名义 fps 和 action horizon 仍为 `50`。

## 正式数据结果

DAgger W3 小批验收：两条真实 Episode 分别得到 2/3 段完整 correction。两条 Episode 全部纳入后，无复制混合索引包含 54 个 segment、27,816 个训练有效样本；完整 LeRobot 已通过 π0.5/OpenPI loader。加权采样尚未启用。

| 项目 | 当前结果 |
|---|---:|
| 已提交源 Episode | 58 |
| success / aborted | 49 / 9 |
| 质量等级 | 38 A / 11 B / 0 C |
| 50 Hz 候选样本 | 40,578 |
| 训练有效帧 | 37,355 |
| 源 success Episode | 49 |
| 连续有效 segment | 50 |
| LeRobot episode | 50 |
| LeRobot RGB 视频 | 150 |
| OpenPI loader | verified |
| 全量 fresh norm stats | verified |
| 8 卡 SFT | completed |

## 当前明确未启用

| 功能 | 状态 | 说明 |
|---|---:|---|
| `bag_timestamp_ns` 时间选择 | disabled | 当前仅留档，不参与配组或 50 Hz selector |
| policy callback-ready 严格在线因果 | disabled | 当前数据没有该时间戳 |
| 图像或关节插值、补帧 | disabled | 只引用真实消息 |
| Depth 模型输入 | disabled | Depth 只用于完整帧组和审计 |
| EEF action | disabled | 当前训练固定为 joint-only |
| 等 EEF 位移 selector 正式数据集 | pending | 代码与 v2 artifact 已完成；尚未在完整数据上重跑 selector、导出和 normalization stats |
| fail/aborted 行为克隆训练 | disabled | 只保留审计，不进入当前训练集 |
| 自动复用 pi05_base 官方 ARX stats | disabled | 当前正式训练使用 fresh stats |
| 原始 MCAP 重写 | disabled | 所有处理均为只读派生 |

## 下一次更新检查单

- [ ] 是否改变了时间选择基准或因果声明？
- [ ] 是否改变了相机/机械臂 age 和配对阈值？
- [ ] 是否改变了 success、idle、运动段或尾裁规则？
- [ ] 是否改变了 state/action 维度、顺序、单位或夹爪标定？
- [ ] 是否改变了 task、fps、action horizon、图像字段或尺寸？
- [ ] 是否生成了新的 filter/dataset version？
- [ ] 是否重新执行 selector、LeRobot 导出和 fresh norm stats？
- [ ] 是否重新通过 LeRobot/OpenPI loader 与真实 batch 验证？
- [ ] 是否更新了正式数据数量和 segment 来源映射？
