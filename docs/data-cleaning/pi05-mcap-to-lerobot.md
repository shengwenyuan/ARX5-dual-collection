# MCAP 到 π0.5 LeRobot 训练集开发与部署计划

- Status: `implementation-verified-awaiting-full-dataset`
- Parent: `meta_plan.md`、`docs/data-cleaning/requirements.md`
- Branch: `main`
- Deployment target: `w3-arx5` 离线数据处理环境
- Input: 已原子提交的 `episode.mcap + metadata.json`
- Output: 可被 openpi 直接加载、计算归一化统计并用于 `pi05_base` 后训练的 LeRobot 数据集
- Current status table: `docs/data-cleaning/pi05-pipeline-status.md`
- DAgger postprocess: `docs/data-cleaning/dagger-postprocess.md`
- Scope note: 已在 `main` 实施离线清洗、π0.5 selector、LeRobot exporter 与验证 CLI；本文件同时记录部署和真实数据验收状态。

## 唯一目标

建立一条可重复、可审计的离线流水线：

```text
原始 Episode
  -> 结构与时间质量审计
  -> openpi 风格训练样本筛选
  -> ARX5 状态与动作构造
  -> LeRobot 数据集导出
  -> openpi 数据加载与 norm stats 验证
  -> pi05_base 后训练输入
```

流水线只服务于 π0.5 后训练，不建设通用数据平台，不修改采集链路，也不改变原始 MCAP。

## 权威基线

实现以 Physical Intelligence 官方 `openpi` 为权威方向，固定参考快照，避免上游变化造成数据格式漂移：

- openpi commit：`15a9616a00943ada6c20a0f158e3adb39df2ccac`
- openpi 锁定的 LeRobot commit：`0cf864870cf29f4738d3ade893e6fd13fbd7cdb5`
- 最小转换模板：[`examples/libero/convert_libero_data_to_lerobot.py`](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/examples/libero/convert_libero_data_to_lerobot.py)
- 双臂转换模板：[`examples/aloha_real/convert_aloha_data_to_lerobot.py`](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/examples/aloha_real/convert_aloha_data_to_lerobot.py)
- idle 筛选实现：[`examples/droid/compute_droid_nonidle_ranges.py`](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/examples/droid/compute_droid_nonidle_ranges.py)
- success/range 筛选加载器：[`src/openpi/training/droid_rlds_dataset.py`](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/src/openpi/training/droid_rlds_dataset.py)
- 数据映射和 π0.5 配置：[`src/openpi/training/config.py`](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/src/openpi/training/config.py)
- action chunk 构造：[`src/openpi/training/data_loader.py`](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/src/openpi/training/data_loader.py)
- 归一化统计：[`scripts/compute_norm_stats.py`](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/scripts/compute_norm_stats.py)
- π0.5 ARX 预训练统计：[`pi05_base/assets/arx/norm_stats.json`](https://storage.googleapis.com/openpi-assets/checkpoints/pi05_base/assets/arx/norm_stats.json)

官方规则与项目适配必须分开记录：官方未给出 MCAP、多速率相机/机械臂或 ARX5 手动拖动示教的直接转换器，相关时间对齐与动作代理属于本项目的显式策略，不伪装成上游规则。

## 范围

实现：

- 批量发现已提交 Episode，验证 metadata、MCAP、Topic、类型与时间线。
- 生成不可变输入对应的质量报告、真实消息引用索引和训练片段清单。
- 应用 success、episode 长度、idle/no-op、有效运动段和 action-chunk 尾部筛选。
- 复用模型无关的 30 Hz 真实帧组索引，将 1 kHz 双臂状态与三路 RGB-D 构造成独立的 50 Hz π0.5 ARX 样本索引。
- 将 YUYV 彩色图转换为 RGB；首版不把 Depth 输入 π0.5。
- 构造双臂 14 维 state/action，导出 LeRobot video dataset。
- 提供 openpi 侧 ARX5 DataConfig、TrainConfig 和训练前验证命令。
- 生成 fresh norm stats，并输出与官方 `pi05_base/assets/arx` 统计的兼容性报告。

不实现：

- 修改、裁剪或重写原始 MCAP。
- 在采集期同步、过滤、插值或降采样。
- 把 fail/aborted 轨迹混入首版行为克隆训练集。
- Depth policy、点云、触觉、EEF action 或多任务混合训练；首版 SFT 只使用双臂 joint action。
- 模型训练框架、GPU 调度、云端上传和 Hugging Face 发布。
- 对原始或派生数据默认生成 SHA。

## 已冻结决策

### 1. 原始数据与派生数据分离

- 原始 Episode 永久保持 `episode.mcap + metadata.json`，处理器只读。
- 清洗不生成“看似更连续”的新 MCAP；先输出审计事实和消息引用。
- 所有排除必须记录 episode、时间范围、原因、策略版本和数量。
- 派生数据先写临时目录，通过验证后原子提交；同一 dataset id 不静默覆盖。

建议目录：

```text
derived/pi05/<dataset_id>/
  audit/<episode_id>/quality.json
  index/<episode_id>/frame_index.jsonl
  selection/sample_index.jsonl
  selection/segments.jsonl
  lerobot/<repo_id>/...
  reports/conversion.json
  reports/openpi-readiness.json
  assets/fresh/norm_stats.json
```

### 2. 训练资格与质量等级解耦

- 所有可读 Episode 均生成质量报告，包括 `success/fail/aborted`。
- A/B/C 只描述结构和时间质量，不由任务结果决定。
- 首版训练 manifest 只接受 `metadata.outcome == "success"` 且满足结构门槛的片段。
- `fail/aborted` 保留审计结果，但不进入 LeRobot 训练数据。

这与现有 `docs/data-cleaning/requirements.md` 的无 oracle 帧索引原则一致；outcome 只在下游训练集合选择层生效。

#### DAgger Episode 选择

- `collection_type=dagger` 必须先通过 `/dagger/authority` 与 metadata control segment 一致性检查。
- 只把 `owner=human` 的半开区间 `[HUMAN_ACTIVE, RESUME_REQUESTED)` 作为专家候选；model、pending、fault 和停止后的 Recorder 尾部仅保留审计，不产生专家 action loss。
- 以 frame group 的 bag timestamp 过滤区间，以 Header timestamp 完成区间内的因果 observation/state 配组；两类时间戳职责不得混用。
- 一个 intervention 对应一个独立的候选 segment 和 LeRobot episode。完整 action horizon 不得越过 segment 末端；不足时删除样本，不 padding、不插值。
- 首版 `pre_roll=0`、`post_roll=0`：frame group 与 action label 都必须位于同一人工区间，不引入边界外模型上下文，也不修改 OpenPI loss mask。
- 普通 demonstration 与 DAgger human segment 可以进入同一训练版本，但必须满足完全相同的 checkpoint-bound 数据契约，并在 manifest 保留来源类型。数据集切分以原始 source Episode 为最小分组，禁止同源片段分散到 train/validation。
- DAgger correction 必须先独立导出并完成 LeRobot/OpenPI 验证。随后通过 `mix-selections` 在索引层与 demonstration 合并，再由唯一 exporter 生成一个混合 LeRobot；不得通过复制视频或样本实现权重。

### 3. π0.5 输入字段

LeRobot 统一字段：

```text
observation.images.cam_high         <- overview color
observation.images.cam_left_wrist   <- left wrist color
observation.images.cam_right_wrist  <- right wrist color
observation.state                   <- float32[14]
action                              <- float32[14]
task                                <- metadata.task.description
```

状态和动作维度顺序固定为：

```text
[left J1..J6, left gripper, right J1..J6, right gripper]
```

- 关节统一为弧度。
- 夹爪统一为 `[0, 1]`，`0` 全开、`1` 全闭。
- `stacking_five_paper_cups_pi05_v1` 以完整 success 数据的可审计观测端点标定：left open `-2.7309837341`、right open `-2.4361028671`、两侧 closed `0`。设备手册中的 `-3.14/-3.4` 候选限位尚未形成可核验配置；未来确认后必须生成新 calibration/dataset version。
- openpi 在模型 transform 中把 14 维补零为 π0.5 的 32 维；数据集本身不预先写 32 维。
- left/right 按机器人背后面向工作区的视角定义，并与 openpi 文档一致。
- RGB 使用 `uint8`，YUYV 解码后检查颜色通道；Depth 保留在审计层，不写入首版训练 features。
- LeRobot 默认使用 video 存储；默认导出分辨率 `640x360`，不裁剪，只缩放并在 manifest 记录变换。

### 4. 50 Hz 因果训练时间轴

为贴近 openpi 的 ARX 预训练控制频率，LeRobot dataset `fps` 固定为 50，π0.5 `action_horizon` 固定为 50，即每个 action chunk 覆盖约 1 秒。

模型无关清洗层仍按 overview 锚点输出约 30 Hz 的 `frame_index.jsonl`，只表达真实三相机帧组及其关联状态。π0.5 层以该索引为只读输入，另行生成 `sample_index.jsonl`；不得把 50 Hz、action、success 或 idle 语义写回通用清洗索引。

每个 20 ms tick：

1. 只选择 `cutoff <= tick` 的最新完整三相机帧组，禁止使用未来图像。
2. 图像帧组最大 age 首版为 `40 ms`；超限 tick 不生成样本。
3. 左右臂分别选择 tick 之前 `2 ms` 内最新的真实 ArmState；不插值关节或夹爪。
4. 30 Hz 图像允许被多个相邻 50 Hz 样本引用，但只复用同一真实图像，不制造新像素或新时间戳。
5. `sample_index.jsonl` 记录 tick、引用的通用 frame group、三路图像源时间戳、双臂状态源时间戳和全部 signed delta。

现有 overview 锚点、跨相机 `±16.7 ms` 配组以及同机 RGB/Depth 完全同 Header 的规则继续作为完整帧组前提。

### 5. 当前 MCAP 的 action 构造

当前采集只有被人工拖动的本体反馈，没有独立控制目标 Topic。首版把演示轨迹上的实测位置定义为 `demonstrated target-position proxy`：

```text
action[t] = [left/right joint_positions, left/right gripper_position] at tick t
```

openpi loader 再从相邻 50 Hz frame 的 `action` 字段构造未来 50 步 action chunk。训练 transform 对 12 个关节维执行相对当前 state 的 delta 转换，两个夹爪维保持绝对值，mask 与官方双臂 ALOHA 配置一致：

```text
[6 joint deltas, 1 absolute gripper, 6 joint deltas, 1 absolute gripper]
```

该代理只适用于当前无从臂、无命令 Topic 的直接拖动示教。实施前必须通过真机单位门槛；若关节不是弧度、夹爪无法稳定映射到 `[0,1]`，不得把数据标记为 training-ready。

本链路明确使用 joint，不生成或训练 EEF action。

### 6. openpi 风格筛选策略

首版策略版本为 `pi05-arx-filter-v1`。

官方方向：

- 只选成功 Episode。
- 排除超过固定长度门槛的 Episode。
- 排除长 idle 段、过短运动段以及运动段尾部 action chunk 大量 idle 的样本。
- 用 q01/q99 检查和归一化 state/action。

本项目具体参数：

- `max_episode_duration_s = 180`；这是项目阈值，因为 π0.5 论文没有公开官方长度值。
- 在单位转换后的 14 维 action 上判断：若相邻 action 每一维绝对差均 `< 1e-3`，该 frame 记为 idle。
- 沿用 DROID 官方时间尺度，而不是照搬其 15 Hz 帧数：
  - 连续 idle 至少 `0.467 s`：排除，对应 50 Hz 下 24 帧。
  - 连续有效运动至少 `1.067 s`：保留，对应 50 Hz 下 54 帧。
  - 每个保留段末尾裁 `0.667 s`：对应 50 Hz 下 34 帧。
- 非有限 state/action、超单位范围、缺 prompt、图像解码失败或 action chunk 越过有效片段边界的样本一律不导出。
- 每个连续 keep range 导出为独立 LeRobot episode，避免删除 idle 后在同一 episode 内形成时间跳变。
- 每个派生 episode 保存 `source_episode_id`、原始起止时间、筛选原因和 policy version 的外部 manifest 映射。

参数必须进入配置和报告，不硬编码在遍历逻辑中。后续调整生成新的 filter/dataset version，不覆盖 v1。

### 7. 归一化策略

- 首个 training-ready 数据集必须运行 openpi `scripts/compute_norm_stats.py` 生成 fresh q01/q99/std。
- 同时加载官方 `pi05_base/assets/arx/norm_stats.json`，输出逐维顺序、范围和尺度对比。
- 官方 ARX norm stats 只有在关节零位、符号、单位、夹爪尺度和维度顺序全部通过兼容检查后才允许作为第二个训练配置复用。
- fresh stats 与官方 ARX stats 保留为两个明确 TrainConfig，禁止在同一配置中自动回退。
- 任一维 `q99-q01` 或 std 过小、非有限、维度错误时 readiness 失败，不直接开始训练。

## 数据契约

### `quality.json`

至少包含：

- 输入 Episode 身份、metadata schema、MCAP topic/type 摘要。
- 每路 count、首末 Header、频率、gap、重复、逆序和非有限值统计。
- RGB-D 配对、三相机配组覆盖率和 observation cutoff 前状态 age 分布。
- A/B/C、结构性 blocking issues、policy/schema 版本。

### `frame_index.jsonl`

由通用清洗层生成，每行表示一个约 30 Hz 的真实帧组，只引用真实消息：

- overview 锚点与 observation cutoff。
- 三路 RGB/Depth topic、消息序号、Header timestamp、相对 anchor/cutoff 差。
- 左右 ArmState 消息序号、Header timestamp、相对 cutoff 差。
- 不包含 50 Hz tick、outcome、idle、action 或 action chunk，不复制 ROS payload。

### `sample_index.jsonl`

由 π0.5 pipeline 生成，每行表示一个 50 Hz 训练样本：

- tick、引用的 `source_episode_id + frame_group_id` 和图像 age。
- 左右状态引用、14 维 state/action 构造版本和单位版本。
- training eligibility、segment id 和排除原因。
- 只保存来源引用和必要数值，不修改通用 `frame_index.jsonl`。

### `segments.jsonl`

每行描述一个将被导出为 LeRobot episode 的连续片段：

- `source_episode_id`、task、source start/end、tick start/end。
- outcome、quality grade、idle filter 参数和 keep/drop 原因。
- 期望 frame count、是否满足完整 action horizon。

### `conversion.json`

至少包含：

- dataset id、repo id、openpi/LeRobot commit、转换代码版本和配置版本。
- 输入/输出 episode 与 frame 数、每个排除原因计数。
- 图像颜色、尺寸、视频编码和 fps。
- state/action 字段顺序、单位、夹爪标定版本和 action proxy 版本。
- 所有输入派生文件路径；本地可重建流程默认不生成 SHA。

## 解耦与复用原则

采集主线以原子提交 `episode.mcap + metadata.json` 为终点；后处理只消费已经提交的 Episode，不导入或调用采集 Session、生产编排、CAN、相机 Source、Recorder、Monitor 或设备探测逻辑。

代码分为两层：

1. 模型无关清洗层负责 MCAP 读取、结构审计、时间线、真实帧配组、质量分档和不可变索引。该层不知道 π0.5、LeRobot、success 训练资格、action horizon 或归一化统计。
2. π0.5 dataset pipeline 只读通用清洗产物，负责 success/idle 选择、50 Hz sample、action proxy、RGB 导出、LeRobot 和 openpi readiness。它不能反向改变 A/B/C 或通用索引。

依赖也按层隔离：

- `cleaning` 只依赖项目数据契约和离线 MCAP/ROS 消息解码能力。
- LeRobot、视频编码和 openpi 兼容代码只允许进入 `pi05_dataset` 可选依赖与离线环境，不进入采集镜像的必需运行路径。
- 共享逻辑使用纯数据对象和显式 Port；不得通过导入 `production` 或 `episode.runtime` 复用副作用代码。
- 未来其他训练链路复用 `quality.json + frame_index.jsonl`，新增自己的 selector、sample index、action adapter 和 exporter，不复制 MCAP 审计与时间配组实现。
- 某条下游 pipeline 的筛选策略、依赖或失败不得改变原 Episode，也不得阻塞采集主线发布。

该边界使本计划既是 π0.5 的具体实现方案，也是后续模型数据链路的参考架构；通用层保持稳定，下游策略按模型独立版本化。

## 代码与模块边界

计划在 `main` 新增：

```text
src/arx5_collection/cleaning/
  models.py       # Issue、Quality、FrameGroup、Policy 契约
  reader.py       # MCAP 顺序读取与 ROS 消息解码
  timeline.py     # 单流时间审计与公共区间
  pairing.py      # RGB-D、三相机和双臂因果关联
  policy.py       # 仅结构与时间质量 A/B/C
  store.py        # 派生索引和报告原子提交

src/arx5_collection/pi05_dataset/
  config.py       # fps、字段、单位、图像和版本配置
  selection.py    # success、idle、keep range 与 sample index
  actions.py      # 14 维 state/action 与 delta mask 契约
  images.py       # YUYV -> RGB、缩放和颜色检查
  exporter.py     # segment -> LeRobotDataset
  validate.py     # 数据集结构和统计验证
  manifest.py     # source lineage 与 conversion report

src/arx5_collection/dataset_cli.py
schemas/quality-v1.json
schemas/frame-index-v1.json
schemas/pi05-segment-v1.json
schemas/pi05-conversion-v1.json
tests/cleaning/
tests/pi05_dataset/
```

CLI 计划：

```text
arx5-dataset clean --input-root ... --derived-root ... --policy ...
arx5-dataset to-lerobot --derived-root ... --dataset-id ... --repo-id ...
arx5-dataset validate-pi05 --dataset-id ... --openpi-root ...
```

清洗与转换可以与采集代码位于同一 Python distribution，但模块依赖保持单向，并作为独立离线进程部署；不放入采集 Session，不连接设备、不启动 CAN/相机或 ROS Source。

## openpi 侧适配

固定 openpi worktree 保持 detached、无业务补丁；ARX5 适配保留在本项目：

```text
src/arx5_collection/pi05_dataset/openpi_adapter.py
scripts/cloud/compute_pi05_norm_stats.py
scripts/cloud/train_pi05_arx5.py
```

适配要求：

- repack 三路图像、14 维 state、14 维 action 和 task prompt。
- 输入图像映射到 `base_0_rgb/left_wrist_0_rgb/right_wrist_0_rgb`。
- joints 使用 `DeltaActions(make_bool_mask(6, -1, 6, -1))`，gripper 保持绝对值。
- `Pi0Config(pi05=True, action_dim=32, action_horizon=50)`。
- 一个配置使用 fresh norm stats；另一个配置显式使用 `pi05_base/assets/arx`，仅在兼容报告通过后启用。
- 初始化权重固定为 `gs://openpi-assets/checkpoints/pi05_base/params`。

训练交接命令：

```bash
source /workspace/ARX5-dual-collection/scripts/cloud/pi05_env.sh
python scripts/cloud/compute_pi05_norm_stats.py --repo-id <lerobot_repo_id>
python scripts/cloud/train_pi05_arx5.py --repo-id <lerobot_repo_id> --exp-name <name>
```

入口动态构造同一 TrainConfig 并直接复用官方 norm stats/data loader/train 函数，不修改 openpi 全局 config registry。训练本身不属于本计划验收范围；计划只保证数据和配置能够到达上述入口。

## 实施顺序

### 当前实现进度（2026-08-17）

- `cleaning` 与 `pi05_dataset` 两层代码、四份 JSON schema、`arx5-dataset` CLI 和独立 dataset 镜像已落在 `main` 工作树。
- w3 独立部署目录为 `/home/lenovo/swy/ARX5-dual-collection-dataset`；原始数据以只读方式挂载，未触碰采集容器和 feat 开发树。
- `cups_overfit-02`～`cups_overfit-07` 共发现 58 条已提交 Episode；49 条 `success` 进入审计，9 条 `aborted` 未进入训练集。质量分档为 38A/11B，无 C。
- 当前 v2 task 固定为 `Stacking paper cups`；混合时 task 集合属于硬契约，不允许只靠大小写或人工理解合并不同 prompt。
- 最终数据集已落到 `/home/lenovo/swy/ARX5-dual-collection-dev/reports/w3/2026-08-16/lerobot/local/stacking_five_paper_cups_pi05_v1`，包含 50 个 parquet 和 150 个 AV1 视频。
- LeRobot 重开验证和固定 commit openpi loader 验证均通过；openpi 输入为三路 `224x224x3` RGB、32 维 padded state、`50x32` action chunk 和有效 prompt。
- 最终 π0.5-base SFT 在 NVIDIA RPBZZZ6 8 卡云端执行；w3 只承担转换和 CPU 可完成的数据验收。

### M0：物理单位与 action 契约门槛

1. 真机确认六关节位置单位、正方向、零位和 left/right 顺序。
2. 标定两只夹爪 raw open/closed 值和方向，冻结 `[0,1]` 转换版本。
3. 对短 Episode 验证 `action[t] = measured position[t]` 的轨迹连续性。
4. 与官方 π0.5 ARX norm stats 做初次逐维比较。

未通过 M0 不进入大批转换。

### M1：清洗契约与 MCAP Reader

1. 实现 schema、模型和 policy config。
2. 复用现有 Header CDR 审计能力，增加完整 ArmState/Image 解码。
3. 实现八路输入验证、时间线、RGB-D 与三相机帧组。
4. 输出确定性的 `quality.json + frame_index.jsonl`。

### M2：50 Hz sample index 与 openpi 筛选

1. 只读通用 `frame_index.jsonl`，从真实帧组构造因果 50 Hz tick。
2. 关联过去 2 ms 内最新双臂状态并记录 age。
3. 构造 14 维 demonstrated target-position proxy。
4. 应用 success、长度、idle、最短运动段和尾部裁剪策略。
5. 将连续 keep ranges 写入 `segments.jsonl`。

### M3：LeRobot 导出

1. 锁定 openpi 指定 LeRobot commit 和隔离依赖环境。
2. 实现 YUYV -> RGB、三相机缩放和 video writer。
3. 按固定 LeRobot commit 的实际 API，将 `task` 随每帧传给 `add_frame`，逐 segment 调用无参数 `save_episode()`；该版本没有额外 `consolidate` 调用。
4. 写 conversion manifest，并重新打开导出数据集进行结构验证。

### M4：openpi 训练前适配与验证

1. 实现 joint-only ARX5 policy/data config 和 fresh/arx-assets 两个 TrainConfig；默认 RPBZZZ6 配置使用 8-way FSDP。
2. 用 openpi loader 读取真实数据，验证 prompt、三图、state 和 action chunk。
3. 运行 `compute_norm_stats.py` 并生成兼容报告。
4. 执行有限 batch 的数据加载和 loss smoke test，不启动正式训练。

### M5：main 部署与小批验收

1. 代码合入 `main` 后构建独立非 privileged dataset 镜像/环境。
2. 在 `w3-arx5` 对 5～10 条成功 Episode 运行完整流水线；SFT 与 GPU loss 验收转移到 RPBZZZ6 8 卡云端。
3. 输入根只读挂载，派生根独立可写；失败保留报告和临时现场。
4. 小批验收通过后再转换完整批次。

## 测试计划

### 单元测试

- MCAP 缺 Topic、错类型、空包、坏 metadata、重复/逆序/无效 Header。
- RGB-D 孤帧、跨相机容差边界、状态 age 边界和公共区间为空。
- YUYV 色条转换、尺寸、RGB 通道、解码失败。
- 14 维顺序、弧度、夹爪 `[0,1]`、float32 和非有限值。
- 通用 30 Hz frame index 与 π0.5 50 Hz sample index 的边界、图像因果复用、未来 observation 禁止。
- idle 段边界、短运动段、尾部裁剪和连续 range 拆分。
- 同一输入和配置生成完全一致的索引、segment 和 manifest。

### 集成测试

- 构造小型 MCAP，走完整 clean -> to-lerobot -> reopen。
- 对真实短 Episode 比较索引引用与 ROS 消息原值。
- 验证导出 LeRobot episode 无时间跳变，task 非空，frame count 与 manifest 一致。
- 验证原始 Episode 的内容、目录和 metadata 均未变化。

### openpi 验证

- 固定 commit 的 LeRobot 能打开数据集。
- openpi loader 返回三路 `224x224` RGB、32 维 padded state/action、50x32 action chunk 和非空 prompt。
- fresh norm stats 包含 state/action 的 finite mean/std/q01/q99。
- q01/q99 无维度坍缩；归一化后的有限样本没有异常大值。
- fresh 配置完成至少一个 batch 的 forward/loss smoke test。
- 官方 ARX assets 配置只有兼容报告通过时才执行同等 smoke test。

## 验收条件

计划在以下条件全部满足后才能将状态更新为 `verified`：

- 原始 MCAP 和 metadata 未修改，所有派生输出可由 manifest 追溯。
- 每个舍弃 Episode、片段和 frame 都有明确原因与策略版本。
- 训练集只包含 success 且满足结构、长度和 non-idle 门槛的连续片段。
- LeRobot 数据为 50 Hz、三路 RGB、14 维 ARX state/action 和有效 task。
- state/action 单位、顺序、夹爪标定和 action proxy 已真机确认并版本化。
- 数据集能被锁定版本 openpi 加载并构造 50 步 action chunk。
- `compute_norm_stats.py` 成功，fresh stats 和官方 ARX stats 对比报告已生成。
- π0.5 fresh 配置的数据 batch 与 loss smoke test 通过。
- w3 小批真实 Episode 完整跑通，进程干净退出，无设备或采集进程依赖。

## 部署与回滚

- 功能代码、schema、CLI 和部署定义均在 `main` 维护。
- 数据处理环境与采集运行环境隔离；转换时不需要 privileged、host network 或设备访问。
- 原始 Episode 输入只读挂载；派生 dataset 使用新 dataset id 和原子提交。
- 新版本失败时删除或保留其未提交临时目录即可回滚，不触碰原始 MCAP 和已验证 dataset。
- LeRobot/openpi 依赖升级必须先新建兼容性任务，不能在既有 dataset version 上原地漂移。

## 开放决策与实施前门槛

以下事项不改变总体路线，但必须在对应里程碑前关闭：

1. Vendor 六关节反馈是否已经是弧度，以及与 openpi ARX 零位/符号是否一致；首版按 joint position 原值落盘，云端 SFT 前仍需完成兼容报告。
2. `stacking_five_paper_cups_pi05_v1` 使用完整 success 数据的左右观测最小值作为 open、`0` 作为 closed，超范围拒绝；设备物理限位确认后另起版本。
3. `action[t] = measured position[t]` 是否需要固定一个控制延迟偏移；先以 0 tick 为基线，通过轨迹和小批过拟合比较决定。
4. 官方 ARX norm stats 的 gripper 尺度与公开 `[0,1]` 描述不一致；兼容报告未通过前只使用 fresh stats。
5. `180 s` 长度门槛和默认 `640x360` 导出分辨率在首批真实转换后仅允许通过新 policy/dataset version 调整。

## 验收结果

当前正式 LeRobot 数据转换已验收；训练侧完整 `verified` 仍需云端 fresh norm stats 与真实 batch smoke：

- 代码层：14 个 cleaning/π0.5 单元测试已在云端最终环境中通过；源代码可编译，其中包含 prompt Repack 回归测试。
- 数据层：49 条 success 源 Episode 完成只读审计和正式 selection；9 条 aborted 排除。统计为 40,578/37,355 candidate/eligible frames、50 个 segment/LeRobot episode。
- 依赖层：LeRobot 固定到 `0cf8648...`，openpi 源码固定到 `15a9616...`；采集镜像和运行中采集容器未修改。
- LeRobot/openpi：最终 37,355-frame 数据重开成功；结构验证为 50 Hz、三路 RGB、14 维 joint state/action、唯一 task；openpi 输出三路 `224x224x3`、32 维 padded state 和 `50x32` action。
- 交付位置：`2026-08-16/lerobot/local/stacking_five_paper_cups_pi05_v1`；数据和转换报告均为 `lenovo:lenovo` 所有权。
- norm stats：100-frame smoke 已证明工具链可用；最终数据的 fresh stats 留到 RPBZZZ6 训练环境计算，避免生成与正式训练环境不一致的统计。
- 云端层：8×RPBZZZ6 已完成 JAX 8 卡 collective、`pi05_base` 参数加载、单卡完整 train step 和 8-way FSDP train step；固定 openpi lock 上只覆盖 NVIDIA NCCL 2.26.5 补丁版本。完整记录见 `docs/deployment/pi05-cloud-training.md`。
- 待完成：将最终 LeRobot 数据同步/挂载到 RPBZZZ6，计算全量 fresh norm stats，并用真实 LeRobot batch 执行单步 loss smoke；这不属于本次 MCAP→LeRobot 交付门槛。

### w3 部署入口

独立镜像包含当前源码，运行时不得覆盖镜像原有 ROS `PYTHONPATH`。推荐直接使用：

```bash
docker build -f docker/Dockerfile.dataset -t arx5-dual-collection:dataset .
docker run --rm \
  -v <episode-root>:/data:ro \
  -v <derived-root>:/out \
  arx5-dual-collection:dataset \
  arx5-dataset clean --input-root /data --output-root /out/audit
```

后续依次调用 `select-pi05`、`to-lerobot`、`validate-pi05`、`validate-openpi` 和 `compute-openpi-norm-stats`；正式参数与结果均保存在 `selection.json`、`reports/conversion.json` 和 fresh `norm_stats.json`。

`docker/Dockerfile.openpi-validation` 只用于 w3 的 CPU 数据验收，将官方 CUDA JAX 替换为同版本 CPU JAX；该镜像不是 RPBZZZ6 训练镜像。
