# 云端 Episode 流式转换与 LeRobot Fragment 计划

- Status: `unit-4-verified`
- Date: `2026-08-25`
- Parent: `docs/data-cleaning/pi05-mcap-to-lerobot.md`
- DAgger recipe: `docs/data-cleaning/dagger-postprocess.md`
- DAgger outcome routing TODO: `docs/episode-runtime/dagger-outcome-directory-todo.md`
- Scope: 云端 Episode 选择、暂存、并行转换、Fragment 提交与最终 LeRobot 组装
- Non-goal: 修改采集链路、原始 MCAP 或既有清洗/选择语义

## 目标

新增一个独立离线入口，以 source Episode 为最小作业单位，端到端完成：

```text
用户白名单路径
  -> 只读发现完整 Episode
  -> 显式目录对齐并等待 ENTER
  -> 冻结本次 source manifest
  -> 建立可恢复作业清单
  -> 逐 Episode 暂存与并行转换
  -> 每个 source Episode 提交独立 Fragment
  -> 确定性组装一个不可变 LeRobot snapshot
  -> 运行现有 LeRobot/OpenPI 验证
```

外部入口暂定为：

```bash
arx5-dataset stream-to-lerobot \
  --config <cloud-conversion.toml> \
  [--output /absolute/lerobot/path]
```

入口保持薄层，只负责加载配置、构造组件、呈现对齐结果和接收确认。路径发现、调度、单 Episode 转换、Fragment 提交及最终组装不得堆入 CLI。

## 已知执行环境

首版开发与 smoke 均在 `ssh pi05-cpu` 上执行，不连接或干扰 W3/W4：

```text
BOS 只读输入挂载：/mnt/bos/datainfra-demo
PFS 中转与产物根：/mnt/pfs/swy/
```

实际输入允许多层目录：

```text
/mnt/bos/datainfra-demo/<任务名>/<日期>/<episode>/
/mnt/bos/datainfra-demo/<任务名>/<日期>/<其他子目录>/<episode>/
```

其中“其他子目录”可能是 `test`、`logs`、`fail`、`dagger_fail` 等。目录名只参与选择和排除；训练语义仍以 Episode metadata 与 MCAP 内容为准。

当前开发测试环境为普通 Linux 云 CPU 容器，cgroup 配额为 `25 CPU / 120 GiB`。执行环境只提供 POSIX 文件系统、BOS 只读挂载和 PFS 可写挂载；本模块不得依赖任何云平台 API、UDF、Workflow、任务协议或平台 SDK。正式全量执行时由用户提高资源配额，代码只读取显式 `workers` 配置，不写死开发环境并发数。

## 冻结原则

1. source Episode 是暂存、清洗、转换、失败重试和 lineage 的最小单位。
2. BOS 源端不要求 commit marker。目录中同时存在 `episode.mcap + metadata.json` 即进入候选集。
3. 并行发生在不同 source Episode 之间；单个 Episode 内继续使用既有顺序 MCAP reader。
4. Worker 的内存和本地缓存上界只依赖单个 Episode，不依赖总 Episode 数量。
5. 每个 Worker 只能写自己的临时目录和 Fragment；最终 LeRobot 由单一 Builder 写入。
6. 原始云端 Episode 永远只读；转换失败不得修改、移动或删除源对象。
7. 清洗、等 EEF 距离采样、DAgger authority 分类、task 和 gripper 契约完全复用现有实现。
8. task 原样保留。同一 source Episode 与同一 source Session 的一致性规则不变。
9. train/validation 继续以 `source_episode_id` 为不可拆分组，禁止同源泄漏。
10. 默认不计算内容 SHA。一次运行内的身份使用规范 source path、文件大小、mtime 和 recipe version。
11. 白名单决定允许发现的目录边界；黑名单只在该边界内按目录名精确剪枝。
12. 同一次请求的白名单路径必须两两互斥：规范化后不得相同，也不得存在祖先/后代包含关系。
13. 对齐阶段不复制 Episode、不创建 Fragment、不启动 Worker；只有用户输入空行 ENTER 后才能执行。
14. 确认后冻结精确的 Episode 清单。Worker 只消费该清单，不在运行中重新发现目录。

## 源文件完整性边界

源端没有、也不计划增加 commit marker。目录对齐由用户显式确认；系统不持续监听目录，也不把目录出现事件当作自动触发信号。

- Discovery 只检查 `metadata.json` 可解析、`episode.mcap` 存在及轻量文件 stat。
- ENTER 后逐 Episode 暂存，再由既有 MCAP reader 和质量契约完整读取。
- MCAP 无法完整读取，或实际内容不符合 metadata 描述预期时，该 source Episode 标记为 `discarded`，不生成训练 Fragment。
- `discarded` 不阻断其他 Episode；最终汇总必须记录 source path、Episode ID、稳定 reason code，以及可用的 expected/observed 摘要。
- 代码异常、PFS I/O 失败等无法判定数据质量的问题标记为 `failed`，不得伪装成数据丢弃。

第一版采用完整 Episode 从 BOS 只读挂载逐条暂存到 PFS staging，再执行顺序扫描。即使清洗需要两次扫描，也只重复读取暂存文件，不反复扫描 BOS。该方案接受“用户确认时目录已经静止”这一操作前提，不增加上传侧协议。

## 云端存储边界

### 原始输入

BOS 作为原始 Episode 的长期存储和分发层，首版根路径固定为 `/mnt/bos/datainfra-demo`。发现范围由用户白名单显式给出，不递归扫描整个挂载：

```text
<source-root>/<task>/<date>/
  <episode-unit>/
    episode.mcap
    metadata.json
  dagger_fail/
    <episode-unit>/
      episode.mcap
      metadata.json
  logs/                         # 可由 block 明确排除
```

约束：

- 一个白名单项可以指向任务、日期或更深的明确子目录。
- 白名单项使用相对 `<source-root>` 的规范路径；拒绝绝对路径、`..` 和解析后逃逸 source root 的符号链接。
- 所有白名单项规范化后必须两两不重叠；例如同时选择 `fold_cloth/2026-08-21` 与其子目录会直接报错。
- 黑名单 `block` 是字符串队列，按路径组件的目录名完整匹配、区分大小写；不做子串、glob 或正则匹配。
- 命中黑名单的目录整棵剪枝，不继续寻找其下 Episode；默认黑名单为空，选择权由用户配置。
- Episode 边界是同一目录内同时存在 `metadata.json` 与 `episode.mcap`；命中后不再向其子目录递归。
- 普通目录和 `dagger_fail/` 都由同一个 `EpisodeLocator` 返回规范候选。
- path、MCAP 文件名中的保留字只用于发现和冲突检查，不作为 collection type 的唯一事实。
- 普通/DAgger 语义最终由 metadata `collection_type`、`outcome` 与 authority stream 共同确认。
- `dagger_fail/` 候选必须验证为 `collection_type=dagger` 且 `outcome=fail`，否则拒绝。
- DAgger aborted 的独立目录标记尚未冻结；采集侧 TODO 完成前不得仅凭现有 `aborted/` 路径把它识别为 DAgger 数据。
- `aborted/` 和普通 `fail/` 默认只审计、不生成训练 Fragment；DAgger fault Episode 仍可按既有规则提取 fault 前已闭合 correction。
- 同一个规范 source Episode 路径只能出现一次；重复发现属于配置错误，不静默去重。
- 同一个 `episode_id` 指向多个云端路径时视为 blocking conflict，不自动选一个。

### 目录对齐闸门

正式执行前必须先完成一次只读对齐：

1. 规范化并校验全部白名单，拒绝相同路径和祖先/后代重叠。
2. 按 `block` 精确剪枝，发现 Episode 边界；只读取轻量 metadata 和文件 stat，不扫描 MCAP。
3. 输出 host、source root、白名单、黑名单、每个白名单的候选数、总 Episode 数、总 MCAP 字节数、task/date/outcome 摘要、排除项和冲突。
4. 输出 PFS staging、Fragment 和最终 LeRobot 目标，并列出候选、预期丢弃项和冲突。
5. 显示：`确认以上目录与 Episode 集合，按 ENTER 开始；输入其他内容或 Ctrl+C 取消：`。

只有读取到空行才视为确认。非空输入、EOF、非交互终端或 Ctrl+C 均应无副作用退出；首版不提供 `--yes` 绕过。确认后才将精确候选写为 `selection_manifest.jsonl` 并启动作业。执行期间源文件 identity 发生变化时，该 Episode 失败，不用重新发现结果替换它。

选择边界只作用于本次运行：

- 同一次运行内，同一 source Episode 禁止重复出现。
- 白名单祖先/后代重叠属于配置错误，避免同一子集被重复暂存和整合。
- 每次新运行都从本次冻结 manifest 现场制作一个完整的新 LeRobot，不跨运行复用历史 Fragment。
- 只有显式恢复同一个 `run-id` 时，才复用该运行已经完成的 Fragment，避免中断恢复重复工作。

### 工作缓存

首版使用 PFS staging，不直接在 BOS mount 上执行 rosbag2 顺序扫描：

```text
<streaming-root>/<run-id>/
  selection_manifest.jsonl
  jobs.jsonl
  staging/<source-key>/
    episode.mcap
    metadata.json
  fragments/<source-key>/
  reports/
```

一个 Worker 同时只拥有一个 Episode。Fragment 成功提交后清理该 Episode staging；discarded/failed 保留结构化诊断，是否保留源暂存由 profile 控制。若 `pi05-cpu` 后续确认存在更合适的本地 NVMe，只需替换 streaming root，不改变转换接口。

### 派生输出

派生数据优先写入 PFS。若未来最终写回不支持原子目录 rename 的对象存储，则另行实现独立 Storage Adapter；该能力不进入首版。

```text
<streaming-root>/<run-id>/fragments/<source-episode-id>/
  lerobot/                     # 单 source Episode 的独立数据集
  fragment.json
  COMMITTED.json               # run 内最后提交

<lerobot-root>/<dataset-name>_<YYYY-MM-DD>/
  data/
  meta/
  videos/
  reports/conversion.json
  reports/source_manifest.jsonl
  reports/discarded.jsonl
  snapshot.json
```

一个 source Episode 可以产生多个 LeRobot episode，例如多个普通运动 segment 或多个 DAgger correction；它们仍共同属于同一个 Fragment 和 split group。没有训练有效 segment 是合法的 `excluded` 结果，不是假失败。

Fragments 是本次运行的并行中间态。Coordinator 等待冻结 manifest 中所有 Episode 进入 `committed`、`excluded`、`discarded` 或 `failed` 终态后，才把本批 committed Fragments 一次性交给 Builder。Builder 不等待未列入本次 manifest 的其他进程或未来 Episode。存在 `failed` 时默认不组装；只有数据契约可明确判定的 `discarded/excluded` 可以带汇总继续组装。

## Source 适配

首版实现 `MountedEpisodeSource`：

```text
discover(include_paths, block) -> EpisodeCandidate[]
stage(candidate, pfs_staging_dir) -> StageReceipt
```

它只允许从配置的只读 source root 读取，逐 Episode 复制 `episode.mcap` 与 `metadata.json`，不复制整段任务、日期目录或未冻结契约的旁车文件。复制先写目标同级隐藏临时目录，文件 identity 和大小复核通过后再原子提交 staging。

`BcecmdEpisodeSource` 降为未来无挂载环境的备选 Adapter，不进入 `pi05-cpu` 首版实现，也不要求凭据或 bcecmd 配置。Coordinator 与 Worker 只依赖 `EpisodeSource` Port，因此未来切换传输方式不影响选择、转换和 Fragment 契约。

## 并行模型

首版使用 Python `multiprocessing`/`ProcessPoolExecutor` 的 `spawn` context，不使用 `fork`：

- rosbag2、rclpy、ffmpeg 和 LeRobot writer 均在 Worker 子进程内初始化。
- 不在父进程初始化 ROS context 后 fork。
- 每个 Worker 一次处理一个 Episode。
- `workers` 是 profile 参数，不由代码写死。
- Coordinator 是 manifest 与状态的唯一写者。
- Worker 只通过结构化 `EpisodeJobResult` 返回成功、排除或失败结果。

```text
Coordinator
  ├─ Whitelist / block validation
  ├─ Read-only discovery / alignment gate
  ├─ Frozen selection manifest
  ├─ Job manifest / resume
  ├─ spawn Worker 1 -> Episode A -> Fragment A
  ├─ spawn Worker 2 -> Episode B -> Fragment B
  ├─ spawn Worker N -> Episode N -> Fragment N
  └─ Builder waits current batch -> complete LeRobot
```

并发上界由以下资源共同决定：

```text
PFS staging capacity
BOS mount read bandwidth
MCAP decode CPU
JPEG/video encoder CPU
PFS/BOS write bandwidth
open file limit
```

第一版不引入 Ray/Daft 或外部任务平台。单节点多进程是正式架构边界；若未来确需跨节点执行，另立平台无关的 Job Executor Port，不改变单 Episode 函数和 Fragment 契约。

## 单 Episode 转换

`convert_episode_fragment()` 是 Worker 唯一业务入口：

```text
stage receipt
  -> validate metadata / complete MCAP contents
  -> clean
  -> optional classify-dagger
  -> route selection recipe
  -> build one-Episode selection artifacts
  -> export standalone LeRobot Fragment
  -> validate Fragment
  -> write fragment manifest
  -> commit
```

路由规则：

| 来源 | 处理 |
| --- | --- |
| demonstration + success | 既有 clean + π0.5 selector |
| dagger + success | clean + classify-dagger + correction selector |
| dagger + fail / `dagger_fail` | 只保留 fault 前完整闭合 correction |
| aborted | 审计并排除 |
| 普通 fail | 审计并排除 |
| MCAP 不可完整读取，或 metadata/Topic/时间线不一致 | discarded，不猜测 |
| 程序、权限、PFS 或其他基础设施异常 | failed，阻止 Builder |

复用要求：

- 不复制 MCAP reader、时间配组、action、gripper、idle 或 DAgger authority 代码。
- 现有 dataset 级 selector 需要拆出单 Episode 纯函数，旧批量入口继续复用它。
- 现有 RGB exporter 已按 source Episode 建立临时缓存；Fragment Worker 继续复用其图像解码和颜色契约。
- `read_episode_scan()` 首版仍允许保留一个 Episode 的 refs 和 arm samples；它不会随总 Episode 数增长。
- 如果未来单 Episode 本身超过内存预算，再把 refs/arm samples spill 到 Arrow/SQLite；这不作为千 Episode 扩容的首版前提。

## Fragment 契约

每个 `fragment.json` 至少记录：

- source path、task/date 路径、source Episode ID、source Session ID。
- collection type、outcome、task 原文。
- 源对象版本或 ETag（若可用）、大小和更新时间。
- cleaning、selection、state/action、gripper、image、OpenPI 和 LeRobot 契约版本。
- source segment 数、导出 LeRobot episode 数和 frame 数。
- split group 固定为 source Episode ID。
- `success`、`excluded` 或 `failed` 状态及稳定 reason code。
- Fragment 路径和提交时间。

Fragment 必须独立通过结构验证后才写 `COMMITTED.json`。临时输出不得被 Builder 发现。

被丢弃的 source Episode 不创建空 Fragment，只向 run report 追加一条结构化记录。最终 `conversion.json` 必须闭合：`selected = committed + excluded + discarded + failed`。

### LeRobot 版本门槛

当前 exporter 与既有 π0.5 训练链路已经实测冻结为官方 LeRobot v2.1，首版 Fragment 与 Builder 必须沿用同一 pinned LeRobot commit 和 v2.1 契约。

实施前必须做一次格式门槛：

1. 用单 Episode v2.1 Fragment 通过当前 OpenPI loader。
2. 确认 pinned LeRobot 是否提供满足确定性重建全局索引要求的稳定合并接口。
3. 若没有稳定接口，使用项目自有、平台无关的 v2.1 reindex/copy Builder，不改变训练语义。
4. 不允许静默升级数据格式，也不依赖任何云平台合并器。
5. 未来适配官方 LeRobot v3 时新增显式 Builder backend、recipe/schema namespace 和完整 OpenPI 回归验收；v2.1 backend 继续可复现，不原地改写。

## 最终组装

Builder 是单写者。它等待本次冻结批次的所有 Worker 到达终态，只读取该 run 的已提交 Fragment：

1. 按 `source_episode_id` 确定性排序。
2. 校验所有 Fragment 的 fps、features、图像、state/action、filter、gripper 和 sampling contract 完全一致。
3. 校验 source Episode 内 task 一致、source Session 内 task 一致，task 字符串原样保留。
4. 重建全局 episode/frame/video/task 索引，不复制或改写训练语义。
5. 输出全局 source manifest，保留 Fragment 和派生 LeRobot episode 的映射。
6. train/validation split 只按 source Episode 分组。
7. 在临时 snapshot 运行现有 `validate-pi05` 与 OpenPI loader。
8. 汇总所有 discarded/excluded；若存在 failed 则停止组装并保留 run report。
9. 全部通过后原子提交 PFS 目录。

Builder 不在 Worker 运行期间写最终数据集，也不观察本次 selection manifest 之外的 Fragment。每次新运行现场构建一个新的完整 LeRobot，历史输出不追加、不覆盖。

最终 LeRobot 原子提交并完成验证后，先确认 `source_manifest.jsonl`、`discarded.jsonl` 和 `conversion.json` 已进入最终输出，再删除本次 `<streaming-root>/<run-id>` 下的 staging 与 Fragments。运行失败或 Builder 未完成时保留该 run，供显式恢复。清理失败只报告 warning，不回滚已经验证并提交的 LeRobot。

### Future TODO：immutable incremental build

数据规模显著增大后再实现，不进入首版：

- Episode Fragment 改为按 source identity 与 recipe contract 持久化保存，不在成功组装后删除。
- 已提交 Fragment 保持 immutable；新增 Episode 只生成新的 Fragment。
- Builder 从明确选择的历史 Fragment 与新增 Fragment 构建一个新的 immutable LeRobot snapshot。
- 历史 snapshot 不追加、不覆盖；recipe 或 schema 改变时进入新 namespace，不静默复用不兼容 Fragment。

## 可恢复状态

Coordinator 维护一个 append-only job manifest，状态机固定为：

```text
discovered
  -> staging
  -> converting
  -> validating
  -> committed

任意阶段 -> excluded
任意阶段 -> discarded
任意阶段 -> failed
```

恢复规则：

- 仅在显式恢复同一个 `run-id` 时，已提交 Fragment 才跳过。
- 未提交的隐藏临时 staging 且没有活动 lease：清理后重试，不发布。
- source identity 在确认后改变：标记 `discarded/source_changed_after_confirmation`，不重新发现或替换。
- failed：保留结构化错误，可按稳定 reason code 选择重试。
- excluded/discarded：同一 run 恢复时不重复转换。
- Coordinator 崩溃不影响已经提交的 Fragment。

不以进程 PID 作为跨节点 lease；首版单 Coordinator。未来分布式执行时再引入带 TTL 的外部 lease。

## 配置

复杂参数统一进入版本化 TOML profile。CLI 只保留配置路径和可选的绝对输出覆盖：

```toml
schema_version = 1

[source]
root = "/mnt/bos/datainfra-demo"
include_paths = [
  "folding_the_cloth/2026-08-21",
  "folding_the_cloth/2026-08-22",
]
block = ["aborted", "fail", "test", "logs"]

[runtime]
streaming_root = "/mnt/pfs/swy/dataset/1011/arx5/fold_cloth/streaming"
workers = 4

[output]
lerobot_root = "/mnt/pfs/swy/dataset/1011/arx5/fold_cloth/lerobot"
dataset_name = "fold_cloth"
repo_id = "<owner>/<dataset>"

[recipe]
name = "pi05-equal-eef-v2"
profile = "<existing-recipe-config>"
task = "folding the cloth"
```

`include_paths` 是本次 LeRobot 的显式来源，不再另设隐式日期参数。`block` 默认空列表，示例值只是配方选择，不是代码内建规则。

首版 recipe 的 `task` 是临时兼容字段，字符串原样进入全部训练 Segment。真实 Episode metadata 目前仍写入通用采集描述；采集侧修复见 `docs/episode-runtime/task-description-todo.md`。该 TODO 验收前不允许从目录名猜测 task；验收后再以显式 schema 版本迁移到逐 Episode metadata task。

未提供 `--output` 时，默认输出为 `<lerobot_root>/<dataset_name>_<YYYY-MM-DD>`，日期取运行当天。`--output` 必须是绝对路径。默认路径或覆盖路径已存在时直接拒绝，不自动覆盖、合并或添加随机后缀。

## 代码边界

计划新增：

```text
src/arx5_collection/streaming_conversion/
  models.py          # EpisodeCandidate、Job、Receipt、Result、FragmentManifest
  config.py          # TOML profile 与资源限额
  source.py          # EpisodeSource Port、MountedEpisodeSource
  discovery.py       # 白名单、block、Episode 边界和重复身份检查
  alignment.py       # 只读摘要、ENTER 闸门、selection manifest 冻结
  worker.py          # convert_episode_fragment()
  fragment.py        # Fragment 验证和原子提交
  coordinator.py     # spawn pool、背压、resume、唯一 manifest writer
  builder.py         # 当前 run 的 committed Fragments -> 完整 LeRobot

src/arx5_collection/dataset_cli.py
  # 只新增 stream-to-lerobot 参数解析和 application wiring
```

不得创建一个同时包含挂载访问、MCAP、selector、LeRobot 和进程池逻辑的“大类”。Source 层只认识 Episode 文件，转换层只认识暂存 Episode 目录，Builder 只认识当前 run 的 committed Fragment。

流式 discovery 可以迁移或复制 `tools/summarize_episode_duration.py` 的简短遍历逻辑，但由流式模块独立维护；两个入口距离较远，不为消除少量重复而强行增加公共抽象。两处都保持 `metadata.json + episode.mcap` Episode 边界和目录名精确 block 语义。

## 测试与验收

### 单元测试

- 白名单相同、祖先/后代重叠、越过 source root 和符号链接逃逸均被拒绝。
- `block` 只做区分大小写的完整目录名匹配；命中后整棵剪枝。
- 任意子目录深度均能发现 Episode，命中 Episode 边界后不继续下探。
- 同一规范 Episode 路径重复出现、同 ID 不同路径均被拒绝。
- ENTER 前不创建 staging、manifest、Fragment 或 snapshot；非空输入、EOF 和非 TTY 无副作用退出。
- 确认后 Worker 只消费冻结 manifest；源 identity 改变则 discarded，不静默替换。
- 普通目录、DAgger 目录和 `dagger_fail/` 路由正确。
- duplicate Episode ID、metadata/path 冲突和非法 DAgger fail 被拒绝。
- MountedEpisodeSource 的隐藏临时目录、复核、提交与清理行为正确。
- spawn Worker 不共享 ROS/LeRobot writer 状态。
- committed/excluded/discarded/failed 的同 run resume 行为幂等。
- source Session/task 与 split group 规则保持现有语义。
- Builder 拒绝任意 recipe/features/fps/gripper 漂移。
- Builder 成功后删除 run staging/Fragments，并确认最终 manifest 与 discarded report 仍完整；失败时保留 run。

### 本地链路测试

1. 用临时挂载 fixture 准备普通 success、普通 aborted、DAgger success、DAgger fail 和损坏 Episode。
2. 至少 2 个 Worker 并行转换，验证每个 staging 隔离。
3. 人工中断 Coordinator 后恢复，已提交 Fragment 不重做。
4. 组装 snapshot，核对 source Episode、segments、frames 和 task 映射。
5. 运行现有 LeRobot validator 和 OpenPI loader。

### `pi05-cpu` smoke

1. 通过 `ssh pi05-cpu` 启动，只读检查 `/mnt/bos/datainfra-demo`，不接触 W3/W4。
2. 白名单选择 2 条普通 Episode 和 1 条 DAgger Episode 所在的互斥路径，并配置明确 block。
3. 核对目录对齐摘要；ENTER 前确认 PFS 无任何新 staging 或输出。
4. ENTER 后两个 Worker 逐 Episode 暂存到 PFS，并行生成 Fragment。
5. Fragment 写 PFS 并提交；核对成功 Episode 的 staging 已释放。
6. 组装一个小 snapshot，并通过 OpenPI sample shape、task 和 action horizon 验证。
7. 模拟中断并恢复同一 `run-id`，已提交 Fragment 应 SKIP；以新 run 重跑则重新完整制作，并拒绝覆盖既有输出目录。

### 千 Episode 验收

- Coordinator 峰值内存不随总 Episode 数线性增长。
- 每个 Worker 峰值内存受单 Episode 上界约束。
- staging 使用量不超过 `workers × max_episode_cache` 加固定余量。
- 单条暂存/转换失败不会取消其他 Episode。
- 进程异常、节点重启后可从 manifest 恢复。
- 最终 snapshot 的 source Episode 数、Fragment 数、派生 LeRobot episode 数和 frame 数可闭合核对。
- 全量 train/validation split 不发生 source Episode 泄漏。

## 实施顺序

1. 冻结白名单互斥、source Episode 判废、run 恢复和 PFS 路径契约。
2. 迁移简洁的 Episode tree iterator，冻结 `EpisodeCandidate`、selection manifest 与 Fragment schema。
3. 实现配置校验、白名单互斥、block 剪枝、只读目录摘要和 ENTER 闸门。
4. 从现有批量 selector 提取单 Episode 纯函数，保持旧入口测试不变。
5. 实现 MountedEpisodeSource、单 Worker、PFS staging 和 Fragment 原子提交。
6. 引入 spawn ProcessPool、背压和 resume manifest。
7. 验证官方 LeRobot v2.1 格式，确定 pinned merge API 或项目自有 v2.1 Builder backend。
8. 实现批次 Builder 与 end-to-end `stream-to-lerobot`。
9. 依次完成本地 fixture、`pi05-cpu` 3 Episode、百 Episode和千 Episode验收。
10. 验证成功清理、失败恢复与同日输出路径冲突保护。
11. 验收结论回写本文件，不在通过前替换现有批量转换入口。

## 开发记录

### 单位 1：Config / Discovery / Alignment

- 本地：`13` 个流式模块定向测试、`21` 个相关回归测试通过。
- 云端：`pi05-cpu` 已确认 `25 CPU / 120 GiB`；两条 BOS Episode 只读发现通过，合计 `18,307,622,138 bytes`。
- ENTER 闸门验证前后均未创建 PFS streaming root、Fragment 或输出目录。
- 已验证白名单互斥、任意深度 Episode 边界、精确 block、重复 ID、metadata/path 冲突和符号链接逃逸。
- 真实 metadata task 是通用采集描述，而既有训练数据通过转换参数写入 `folding the cloth`。首版已冻结为 recipe 显式 task；采集侧逐 Episode task 缺口独立记录在 `docs/episode-runtime/task-description-todo.md`。
- `pi05-cpu` 上 `13` 个定向测试和两条真实 BOS Episode 的无副作用对齐 smoke 通过，单位 1 收口。

### 单位 2：Frozen Manifest / Job State / Resume

本单位只实现确认后的控制面持久化，不暂存 MCAP、不启动转换 Worker：

- 新 run 使用同文件系统临时目录写入 `run.json`、`selection_manifest.jsonl` 与初始 `jobs.jsonl`，完整后一次 rename 提交；已存在 run-id 拒绝覆盖。
- `run.json` 冻结 recipe name/profile/task 与目标 snapshot；selection row 冻结规范 source path、Episode metadata 摘要和 MCAP/metadata size + mtime；不计算内容 SHA，也不在每条 Episode 重复运行级字段。
- `jobs.jsonl` 是单 Coordinator 写入的 append-only 事件流；每次追加后 flush + fsync，恢复时重放并严格验证迁移。
- 正常迁移固定为 `discovered -> staging -> converting -> validating -> committed`。
- 非终态可以进入 `excluded/discarded/failed`；终态不接受普通迁移。
- `failed` 只允许通过显式 retry 进入新 attempt 的 `staging`；`committed/excluded/discarded` 恢复时保持 SKIP。
- reason code 使用稳定的小写路径格式，例如 `discarded/source_changed_after_confirmation`；自由文本 detail 只供诊断，不参与控制判断。
- 本单位的 resume 只恢复冻结选择与当前 Job 状态，不清理未提交的临时 staging、不判断活动 lease；这些属于 Mounted Source/Coordinator 后续单位。
- 本地：`27` 个流式与相关数据处理回归测试通过。
- 云端：`pi05-cpu` 上 `19` 个定向测试通过；两条真实 BOS Episode 完成只读冻结。模拟结果分别恢复为 `committed/attempt=0` 与 `staging/attempt=1`，证明终态 SKIP 和显式 retry 语义正确。
- 云端 smoke 只创建精确命名的临时 PFS run manifest，未复制 MCAP、未创建目标 LeRobot；验证后已清理该临时目录。

### 单位 3：Mounted Source / Atomic Staging

本单位只实现冻结候选到 PFS 的单 Episode 暂存，不读取 MCAP 内容、不运行 selector、不生成 Fragment：

- `MountedEpisodeSource` 显式绑定 source root，只允许读取 root 内的规范路径；输入为冻结的 `EpisodeCandidate`，输出为不可变 `StageReceipt`。
- 首版只复制 `episode.mcap` 与 `metadata.json`。没有已冻结契约的旁车文件不做隐式递归复制。
- 复制前、复制完成后均复核源文件 `size + mtime_ns`；任何变化统一抛出 `SourceChangedError`，由后续 Coordinator 映射为 `discarded/source_changed_after_confirmation`。
- 写入目标同级隐藏临时目录，文件 flush + fsync 后写 `stage.json`，最后复用项目原子目录提交；异常时不留下可见 staging。
- 已存在的目标目录拒绝覆盖。恢复逻辑只能显式复用经过 `stage.json` 和文件 identity 校验的完整 staging；本单位不做自动清理或猜测。
- `StageReceipt` 只记录 Episode ID、规范 source/staged 路径及两份文件 identity，不记录 SHA，也不承载清洗或训练语义。
- 本地：`26` 个流式模块测试、`34` 个相关回归测试通过；覆盖源文件复制前/中变化、目标冲突、损坏 staging、source root 逃逸和异常清理。
- 云端：`pi05-cpu` 从 BOS 只读暂存一条 `9,244,689,012 bytes` Episode 到 PFS，用时 `17.432 s`，实测 `505.770 MiB/s`；`stage.json`、两文件大小及恢复校验均通过。
- 云端 smoke 的精确临时 staging 已在复核后删除；BOS 未写入，W3/W4 未连接。

### 单位 4：Pinned Conversion Recipe / DAgger Fail Selection

本单位先冻结 Worker 将要复用的转换参数，不实现 Worker、Fragment 或 Builder：

- 新增 task 无关的 `pi05-equal-eef-v2` conversion recipe，显式记录 cleaning、等 EEF 采样、左右夹爪标定和 LeRobot v2.1 backend；不再把训练 profile 当转换 profile。
- 流式 profile 的 `recipe.name` 必须与 conversion recipe 一致；`recipe.task` 继续是本批 legacy Episode 的原样 prompt，两者不混为一个配置。
- recipe loader 使用严格 schema 和字段集合，拒绝未知字段、缺字段和隐式版本升级。
- DAgger selector 接受 `outcome=success`，以及已被 authority classifier 判定有效的 `outcome=fail`；两者都只选择完整闭合的 expert correction。
- `outcome=aborted` 继续排除；普通 demonstration fail 不经过 DAgger selector。DAgger aborted 的未来规则沿用独立 TODO，不在本单位扩张。
- 用现有 W3 v2 验收值冻结首版参数；参数变化必须产生新的 recipe name/schema，不原地改变历史转换语义。
- 本地：`63` 个流式、DAgger、π0.5 与清洗相关回归测试通过。
- 云端：`pi05-cpu` 上 `31` 个定向测试通过；严格加载 `pi05-equal-eef-v2 + lerobot-v2.1 + 5 mm + horizon 50` 契约，无 MCAP 读取、PFS 写入或 W3/W4 连接。

## 待补信息

- 两组拟纳入首个 snapshot 的互斥白名单路径样例。
- 首个云端 smoke 使用的普通、DAgger、`dagger_fail` 各一个完整 Episode 路径。

## 参考

- 现有项目计划：`docs/data-cleaning/pi05-mcap-to-lerobot.md`
- 现有 DAgger 后处理：`docs/data-cleaning/dagger-postprocess.md`
- 官方 pinned LeRobot：`0cf864870cf29f4738d3ade893e6fd13fbd7cdb5`
