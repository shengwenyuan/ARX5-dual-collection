# BOS Episode 上传工具 TODO

- Status: `implemented / local verified / live BOS pending`
- Date: `2026-08-26`
- Target: `tools/bos_upload_episodes.py`
- Scope: 本地 Episode 上传前审计、抽样验证、时长汇总、`bcecmd` 上传监督
- Non-goal: 修改 Episode、清洗 MCAP、生成 LeRobot、管理 BOS 生命周期

## 目标

用户粘贴一条或多条标准 `bcecmd bos sync` 命令。工具只解析命令，不通过 shell 执行用户文本；完成全量粗筛、抽样质量门和人工确认后，才按顺序调用 `bcecmd`。

```text
commands
  -> 安全解析与路径展开
  -> 全量 Episode 粗筛
  -> 不合格对象确认迁移至 abort/ 并重新全量扫描
  -> 10% / 最多 15 条抽样深检
  -> 调用 summarize_episode_duration.py
  -> 最终目录与统计确认
  -> 顺序上传、带宽监督、断点重试
  -> BOS Episode 目录与粗筛完整性复核
```

任何 gate 失败都禁止启动上传。BOS 在整个预检阶段只读。

## 命令边界

使用 `shlex.split()` 逐行解析，执行时向 `subprocess` 传递 argv，禁止 `shell=True`。首版只接受：

```text
bcecmd bos sync <absolute-local-directory> <bos:/absolute-prefix/> [approved options]
```

- 禁止管道、重定向、命令替换、环境变量展开和多命令连接符。
- 只允许本地目录上传到 BOS；拒绝 BOS 下载、BOS 间复制和相对本地路径。
- 多条命令的本地来源和 BOS 目标均不得重叠，避免同一 Episode 被重复选择或写入。
- 命令顺序执行，不并发启动多个 `bcecmd`；单条命令内部使用其 `--concurrency`。
- 拒绝 `--delete`、`--deleteSrc`、`--restart`、`force-overwrite` 等破坏性或关闭续传的参数。
- 用户输入的 `--include`、`--exclude` 暂不接受，避免上传集合与审计集合分离。工具在隔离不合格 Episode 后负责向实际 argv 注入精确的 `--exclude 'abort/*'`。
- 解析后展示规范化本地根、BOS 目标、参数和推导出的 Episode 数，用户确认的是实际 argv，不是原始字符串。

## bcecmd 已确认事实

百舸公开文档使用 `bcecmd bos sync <SRC> <DST>` 进行 BOS 目录同步。当前 `pi05-cpu` 上的 `bcecmd v0.5.10` 帮助与只读 dry-run 验证得到：

- `--concurrency` 控制单次 sync 最大并发。
- 默认允许从断点传输；`--restart` 的含义是“不从断点传输”，本工具必须拒绝该参数。
- 当前版本仅接受 `time-size`、`time-size-crc32`、`only-crc32`、`force-overwrite`。
- `dest-not-exist` 在该版本会直接报 `unknown sync type`。
- 官方示例来源：<https://cloud.baidu.com/doc/AIHC/s/7mkp7yugb>。

工具将 `dest-not-exist` 视为默认的“不覆盖”意图：运行时探测当前 `bcecmd` 是否支持，支持则传递，不支持则静默省略并使用默认 `time-size`。无论底层版本如何，上传前都检查目标前缀；发现同名对象时整批拒绝，因此静默降级不会造成覆盖。

## Gate 0：上传集合与静态安全

每个源目录递归寻找 Episode。包含 `episode.mcap` 或 `metadata.json` 任一文件的目录都视为 Episode 候选，缺少另一文件即失败。

- 本地来源必须存在、为真实目录，解析后的所有文件必须位于来源根内。
- 拒绝符号链接逃逸、FIFO、socket、仍在写入的文件和临时文件。
- 同一批次 `episode_id`、规范本地路径和推导 BOS 目标均必须唯一。
- 文件大小和 mtime 间隔复查后必须稳定，避免上传正在录制的 Episode。
- BOS 目标冲突检查在任何本地删除动作之前完成。

## Gate 1：全量粗筛

对全部候选执行成本低、无需完整解码 MCAP 的检查。暂定失败条件：

1. `episode.mcap` 小于 `2 GiB = 2,147,483,648 bytes`，或不是普通文件。
2. `metadata.json` 缺失、JSON 非法或不是 object。
3. `episode_id` 非空性失败，或与目录名不一致。
4. 缺少有效 `schema_version`、`collection_type`、`outcome`、`task.id`、`task.description`、`timing.started_at`、`timing.ended_at`、`timing.duration_s`、`station.id`。
5. `timing.duration_s` 非有限正数，或结束时间不晚于开始时间。
6. 八个 canonical stream id 不完整、不唯一，任一 required stream 的 `message_count <= 0`。
7. `metadata.errors` 非空。
8. 同一 Episode 存在残留录制临时文件，或 MCAP/metadata 在稳定性复查期间发生 size/mtime 变化。

`outcome=success` 只表示操作结果，不构成质量证明；此前 `0.168 s / RGB 0 帧` 的 Episode 正是本 gate 必须挡住的对象。Task 可以在一个上传批次中包含多种原始字符串，不做全局一致性检查、不归一化、不从路径猜测。

粗筛规则必须实现为同一个可复用的纯逻辑入口，输入为 Episode 文件清单、对象大小和 metadata 内容；本地上传前检查与 BOS 上传后检查不得各写一套近似规则。文件系统和 BOS 只负责提供各自的 inventory/read adapter。

粗筛失败时：

- 立即中止上传，以中文逐条输出 Episode 目录、原因和字节数。
- 展示将迁移的规范绝对路径、目标路径及合计大小。
- 只有交互式 TTY 的空行 ENTER 才执行迁移；任何其他输入、EOF 或 Ctrl+C 都取消。
- 只能迁移已冻结、位于某个 source root 内且不是 source root 本身的精确 Episode 目录。
- 统一移动到对应 `<source-root>/abort/<episode_id>/`；目标已存在时拒绝，不覆盖、不自动改名。
- 目录命名只保留 `abort/`。Episode metadata 的 outcome 仍使用既有枚举值 `aborted`，两者不混淆。
- discovery、时长统计和实际 `bcecmd` argv 都显式排除 source root 下的 `abort/`。
- 迁移后从命令解析开始重新扫描；不合格对象未清零时始终禁止上传。

## Gate 2：10% 深度抽样

样本数：

```text
min(15, max(1, ceil(valid_episode_count * 0.10)))
```

选择必须可复现：优先覆盖每个 `station.id + collection_type`，并包含 MCAP 最小、最大项；剩余名额按规范 Episode 路径的稳定排序抽取。Task 多样性只用于覆盖抽样，不要求全批统一。

深检不复制质量算法，直接复用当前 cleaning、等 EEF selector、LeRobot v2.1 exporter 和 validator，在临时目录执行完整转换并在结束后清理。

所有深检阈值必须集中在独立、版本化的 `BosUploadValidationPolicy` 与 `config/bos-upload-validation.v1.toml` 中。CLI 只加载 policy；采样器、命令解析器和上传 supervisor 不得持有图像、时间、coverage 或 action 参数。未来调整超参数时新增或升级 policy，不修改执行编排。

首版硬 gate：

1. MCAP 实际包含三路 RGB、三路 aligned depth 和双臂 state 共八路 canonical Topic。
2. RGB 为 `rgb8 / 848x480`，Depth 保持对齐深度格式与 `848x480`；消息结构和双臂 state 维度合法、数值有限。
3. 单相机 RGB/Depth 同 timestamp 配对；三相机配组容差 `<=16.7 ms`；双臂状态 age `<=2 ms`。
4. 无重复或逆序 Header timestamp；相机最大 gap `<=67 ms`，双臂最大 gap `<=3 ms`。
5. 完整因果帧组 coverage `>=95%`，即 cleaning grade A/B；grade C 拒绝。
6. 等 EEF selector 至少产生一个有效训练 Segment，且能够满足 `action_horizon=50`。
7. 临时 LeRobot Fragment 通过独立 reopen、shape、task、action、source lineage 和索引闭合校验。
8. 同一 source Episode、同一 source Session 内 task 一致；不同 Episode/Session 和最终 snapshot 允许多 task。

任一样本失败即阻断整批上传并输出失败证据，等待人工处理；不提供 ENTER 批量迁移或删除，也不能把 90% 未抽样对象宣称为“已深检”。普通 `demonstration + success`、`dagger + success` 和 `dagger_fail` 都支持，并分别复用已有 selector；不得让不同 collection/outcome 共享错误的训练资格判断。

## 时长汇总与最终确认

粗筛清零且深检通过后，工具按每条已解析 source root 调用现有：

```text
tools/summarize_episode_duration.py --directory <source-root> --block abort
```

不修改或复制该脚本的遍历与时长逻辑。上传工具解析其标准输出，回传每个来源及整批的 Episode 数、总秒数和 `HH:MM:SS.mmm`；统计 Episode 集合必须与本轮冻结上传集合完全一致，否则阻断。

最终确认页同时显示命令数、Episode 数、总 MCAP 字节、总时长、抽样结果、来源到 BOS 的映射和目标冲突结果。再次按 ENTER 后才启动 `bcecmd`。

## 上传监督与恢复

- 每条命令保留 `bcecmd` 原始 stdout/stderr，并额外写入一次 run log；日志不得放进待上传 source root。
- 不传 `--restart`，保留 bcecmd 默认断点能力。
- 首次失败后最多自动重试 `5` 次，退避固定为 `10 s / 30 s / 1 min / 5 min / 15 min`；重试仍执行同一 argv，让 bcecmd 复用断点和已完成对象判断。五次重试全部失败后，本条命令判定为 `stalled`，后续命令不再启动。
- SIGINT/SIGTERM 视为用户主动停止：停止当前子进程并退出，不自动重启。
- 参数、认证、权限、目标冲突等确定性错误不重试；网络超时、连接重置、服务端 5xx 等才进入重试。
- 带宽不设置最低速度硬门，避免有限共享带宽导致误杀；但必须观测滚动吞吐、报告均值与停滞时长。建议连续 `180 s` 无字节进展即终止当前尝试并进入重试。
- `bcecmd` 返回成功后仍不视为完成，必须通过下述 BOS 完整性校验。
- 所有命令成功后输出已上传 Episode 数、字节、时长、重试次数、平均吞吐和失败清单。

## BOS 上传后完整性校验

每条 sync 在上传前已经冻结本地 Episode 的规范相对目录集合。上传后通过 BOS list/head 和读取小型 `metadata.json` 构建远端 inventory；不回传完整 MCAP，不增加 SHA。

1. 冻结集合中的每个 Episode 相对目录必须完整存在于 BOS 目标；目录层级和 Episode ID 均不得缺失、截断或改名。
2. BOS object storage 的“目录存在”定义为目标 prefix 下同时存在 `<episode-relative-dir>/episode.mcap` 和 `<episode-relative-dir>/metadata.json`；不依赖空目录对象。
3. 每个远端 `episode.mcap`、`metadata.json` 的对象大小必须与冻结的本地文件大小一致。
4. 对每个 BOS Episode 读取 metadata，并调用与上传前完全相同的 Gate 1 粗筛逻辑；全部远端 Episode 都必须再次通过。
5. 远端检查同样验证目录名与 `metadata.episode_id` 一致、八路 metadata 完整、MCAP `>=2 GiB`、required stream count 非零、errors 为空和 timing 合法。
6. `abort/` 必须既不出现在冻结上传集合，也不出现在本次 BOS 目标新增对象中。
7. 目标前缀中既有、且不属于本次冻结集合的其他 Episode 不参与本轮验收；若与本轮相对路径冲突，已由上传前目标冲突 gate 阻断。
8. 任一缺失、大小不符或粗筛失败都使本次尝试失败；工具输出中文的相对目录和原因，然后重新执行同一 sync argv，复用既有断点与五次退避策略。
9. 五次重试后仍不能让全部冻结 Episode 在 BOS 中闭合，则该命令标记为 `stalled`，停止后续命令并保留完整日志。

该校验确认名称、对象存在性、大小和 metadata 契约，不宣称内容级字节一致；仍遵循项目默认不做 SHA 的原则。

## 实现边界

```text
tools/bos_upload_episodes.py          # 单一小工具入口与核心编排
config/bos-upload-validation.v1.toml  # 粗筛、抽样、深检与重试参数
tests/test_bos_upload_episodes.py     # 定向契约测试
```

实现没有搭建额外 package 层。`DeepGate` 独立封装可调深检规格；`coarse_issues()` 同时服务本地和 BOS inventory；MCAP 质量算法、LeRobot 转换和时长遍历继续调用既有实现。

## 输入入口

正式入口同时支持 `--commands-file <path>` 和 stdin 多行粘贴；两者进入同一个逐行解析器。交互粘贴以空行结束，commands file 便于审计和复跑。

## 已冻结决策

- 最小 MCAP 为 `2 GiB`。
- 粗筛不合格 Episode 经 ENTER 确认后迁移到 `abort/`，不永久删除。
- BOS 同名目标严格阻断。
- `dest-not-exist` 默认启用；当前 bcecmd 不支持时静默省略，由冲突预检保证不覆盖。
- 深检失败只阻断并等待人工处理。
- 普通 success、DAgger success 和 `dagger_fail` 均支持。
- 命令文件与 stdin 粘贴均支持。
- 网络失败最多重试五次，固定退避后仍失败则判定 `stalled`。

## 本地验收

- 命令解析、`dest-not-exist` 静默降级、`abort/` 迁移、10%/15 条抽样、BOS listing、远端复用粗筛和端到端 fake-bcecmd 链路共 `7` 个定向测试通过。
- 全仓结果：`372 passed, 2 skipped`。
- `bcecmd` 真实凭据上传与远端对象复核尚未执行；首次使用应选择一个小型正式批次完成 live BOS 验收，不用 W3/W4 生产采集进程作为测试对象。
