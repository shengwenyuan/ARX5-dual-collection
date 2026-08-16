# Episode Runtime 实施计划

- Status: `implemented`
- Parent: `meta_plan.md`
- Runtime: Python 3.10+、ROS 2 Jazzy、Ubuntu 24.04 Container
- Target: 先完成无 Vendor SDK 闭环，再接入 `w3-arx5` 真机 Topic

## 目标

实现与 ARX5、RealSense SDK 解耦的 Episode 控制面：接收录制触发，管理状态与异常，协调 MCAP Adapter，生成 metadata，并在完整关闭后原子提交。

SDK 调研只影响数据源适配器，不得阻塞 Episode Runtime 主线。

## 范围

本计划实现：

- Episode 状态机与单实例并发约束。
- 键盘录制触发和脚踏板接口边界。
- `.partial` 目录、metadata、原子提交，以及对 `RecordingBackend` Hook 的调用。
- 消费 StreamMonitor 的统计与必需 Stream 停止事件，执行 `aborted` 路径。
- 本地任务输入、命令行入口、重复 Episode 和异常退出处理。
- Fake Backend、Fake Stream Monitor 及无硬件集成测试。

本计划不实现：

- ARX5、RealSense 数据读取与 Vendor SDK 生命周期。
- 相机同步、Depth 对齐、EEF 坐标或电流单位解释。
- 脚踏板驱动、UI、人工 `fail`、DAgger 和远程任务分发。
- 最终图像编码、MCAP 压缩和磁盘保留阈值。
- ROS 2 Source、固定 Topic、消息定义、真实频率监督和 MCAP Adapter。
- SHA 或通用内容校验。

## 架构决策

### 控制面与数据面分离

- Episode Runtime 使用 Python 实现，负责状态、文件生命周期、metadata 和控制事件。
- Runtime 不 import `pyrealsense2`、`bimanual` 或其他 Vendor SDK。
- 相机和机械臂由独立 ROS 2 Source 发布数据；Runtime 只接收配置化 Topic 与健康事件。
- Runtime 不解析图像、关节或 EEF 载荷，不承担插值、补帧或时间同步。
- Hook 使用少量 `Protocol` 与数据类表达，不建设通用插件框架。

### 分支与目录边界

- `main` 负责 SDK、Docker、ROS 2 Source、固定 Topic、消息定义、频率监督和 MCAP Adapter。
- Episode Runtime 开发分支只负责状态机、Hook、Store、metadata 和 CLI。
- `main` 不修改 `src/arx5_collection/episode/`；Episode Runtime 分支不修改 SDK、Docker 或 ROS 2 Source。
- Hook 合入后，`main` 在 Episode 包之外实现 `RecordingBackend`、`StreamMonitor` 的 ROS 2 Adapter，不实现或修改 Episode 生命周期。

### 状态语义

```text
READY --start--> RECORDING --stop--> FINALIZING --> COMPLETE --> READY
                       \--source/backend error--> ABORTED --> READY
```

- 同一进程同一时间只允许一条活动 Episode。
- 正常 `stop` 生成 `success`。
- 必需 Stream 停止、运行期 Backend 异常或录制中断生成 `aborted`。
- 人工 `fail` 保留在结果枚举中，但 v0.1 不提供输入入口。
- `COMPLETE` 与 `ABORTED` 是结果，不允许从中继续写入原 Episode。

### 文件提交语义

```text
episodes/
  .<episode_id>.partial/
    episode.mcap
    metadata.json
```

- 录制期间持续写磁盘，不缓存整条 Episode。
- Source 异常后若 MCAP 能完整关闭，则提交正式 `aborted` Episode。
- MCAP 关闭、metadata 写入或目录提交失败时保留 `.partial`，不得伪装成正式 Episode。
- 提交使用同一文件系统内的目录原子重命名。
- 正式目录只包含 `episode.mcap` 与 `metadata.json`；默认不生成 SHA。
- 启动时只报告遗留 `.partial`，不自动恢复或删除。

## 外部依赖处理

| 依赖 | 决策 | 边界与替换条件 |
|---|---|---|
| ARX5 与 RealSense 数据源 | main 实现 | Runtime 只读取 `StreamSpec` 和 ROS Topic，通过 Hook 协作，不接触 Source 或 Vendor SDK |
| Topic 与消息类型 | 已冻结 | 六路相机 `sensor_msgs/Image` 与双路逻辑 `ArmState` 由 Station 配置传入，Runtime 不硬编码物理设备身份 |
| Stream 频率与掉线检测 | 已实现 ROS Adapter | `RosStreamMonitor` 位于 Episode 包外，只订阅 `/monitoring/stream_status`；Runtime 通过冻结的 `StreamMonitor` Hook 轮询 |
| MCAP 写入方式 | 已实现 ROS Adapter | `RosbagRecordingBackend` 位于 Episode 包外，使用 Jazzy `rosbag2_py` 与 MCAP；Runtime 只调用冻结的 `RecordingBackend` Hook |
| 键盘与脚踏板 | Hook + 实现键盘 | `RecordTrigger` 是稳定边界；v0.1 实现键盘，脚踏板在设备与驱动确定前完全等待，不创建伪驱动 |
| 任务与 UI | Hook + CLI | CLI 构造 `EpisodeRequest`；未来 UI 调用同一入口，不为 UI 建临时页面或服务 |
| 人工 `fail` | 等待 | 结果 Schema 保留 `fail`，v0.1 不创建临时按键；UI 计划确认后再接入 |
| 相机同步与设备时间戳 | 完全等待 | Runtime 原样录制 Topic，不提供同步 Hook，避免把未确认的 SDK 语义带入控制面 |
| DAgger | 完全等待 | 本计划不预埋控制类；metadata 保留扩展字段即可 |
| 磁盘空间阈值 | Hook + 等待默认值 | 实现可配置 `min_free_bytes` 检查；默认值等待三路 720p 基准后冻结 |
| Station 配置 | 只读接入 | Runtime 只记录配置版本和逻辑设备信息，不启动或操作硬件 |
| Artifact Validator | 外部工具，不作为运行依赖 | Runtime 测试直接断言产物；完整 Validator 可并行开发，完成后只用于验收与离线检查 |
| Container 与部署入口 | 复用现状 | 纯 Core 不依赖 Container；真实 MCAP Adapter 复用现有 Jazzy Image，SDK 镜像变化不进入 Runtime 核心 |

测试桩只能存在于测试或显式开发模式，不得在生产命令中静默替代缺失设备。

## 核心契约

### `EpisodeRequest`

- `task_id`
- `task_description`
- `output_root`
- `station_config`
- `streams: list[StreamSpec]`

### `StreamSpec`

- `id`：稳定逻辑标识。
- `topic`：由外部配置传入。
- `required`：停止时是否触发 `aborted`。
- `expected_hz`：只用于统计和警告，不用于补帧或自动判失败。

### `EpisodeResult`

- `episode_id`
- `outcome: success | fail | aborted`
- 开始、结束与持续时间。
- MCAP、JSON 路径。
- 每个 Stream 的帧数、平均频率、最大帧间隔和警告。
- 运行期错误与提交结果。

上述契约不包含 Vendor 对象。未知单位继续保留在 Source 消息语义中，不由 Runtime 猜测。

### Metadata v1

- `started_at`、`ended_at` 使用 RFC 3339 UTC；`duration_s` 必须由单调时钟计算。
- 不建立全局 `frame_count`、统一 FPS 或统一帧轴。
- 每个 Stream 以 `id`、`topic`、`required` 标识；统计字段仅包含 `expected_hz`、`message_count`、`observed_hz`、`max_gap_ms`。
- `calibration.intrinsics` 与 `calibration.extrinsics` 必须存在，v0.1 固定写 `null`。
- 不记录 `container_image` 或 `committed`；目录是否脱离 `.partial` 即提交事实。
- 核心对象严格校验；只有设备 `configuration` 与顶层 `extensions` 允许开放字段。

## 目录设计

```text
src/arx5_collection/episode/
  models.py
  ports.py
  runtime.py
  store.py
  metadata.py
  cli.py
  adapters/
    keyboard.py

schemas/
  episode-metadata-v1.json

tests/episode/
  fakes.py
  test_runtime.py
  test_store.py
  test_metadata.py
  test_keyboard.py
```

`fakes.py` 只服务测试；不得进入生产 Adapter 目录。

## 实施步骤

### 1. 冻结控制面契约

1. 定义状态、Outcome、`EpisodeRequest`、`StreamSpec` 和 `EpisodeResult`。
2. 定义 `RecordingBackend`、`StreamMonitor`、`RecordTrigger` 三个最小 Hook。
3. 定义 metadata v1 JSON Schema，字段与 `meta_plan.md` 保持一致。
4. 用契约测试锁定正常、异常和重复 Episode 行为。

### 2. 并行开发核心组件

- Workstream A：状态机、键盘触发和 CLI。
- Workstream B：临时目录、metadata、原子提交和遗留目录报告。
- Workstream C：Fake Backend、Fake Monitor、掉线与 Backend 故障注入。

三个 Workstream 只通过 `models.py` 与 `ports.py` 协作，避免同时修改同一实现文件。

### 3. 对接 main 数据面 Adapter

main 负责验证 `rosbag2_py + rosbag2_storage_mcap` 与 ROS Stream Monitor：

1. 录制显式 Topic 列表，不使用全量 Topic 捕获。
2. 连续开始、停止十条 Episode，不重启进程。
3. 验证干净关闭、异常停止和 MCAP 可读取性。
4. 验证最终目录能收敛为 `episode.mcap + metadata.json`。

如果 Adapter 无法满足契约，停止集成并重新对齐；Episode Runtime 分支不得自行实现 ROS 2 Adapter、直接 MCAP Writer 或改变 Episode 格式。

### 4. 无 SDK 集成

1. 使用 Fake ROS Publisher 提供三路 30 Hz 和双路 60 Hz Topic。
2. 使用键盘连续录制多条 Episode。
3. 注入必需 Stream 停止、低频、Backend 异常和 `Ctrl+C`。
4. 测试直接检查 MCAP、JSON、Topic、统计和 Outcome；若 Artifact Validator 已完成，再追加外部验收。

### 5. 真机接入

1. Source 计划冻结 Topic 叶子名称与消息类型后更新 `StreamSpec` 配置。
2. ROS Stream Monitor 可用后替换 Fake Monitor。
3. 在 `w3-arx5` 接入真实 Topic，不修改 Runtime 核心。
4. 根据三路 720p 基准冻结磁盘阈值、编码和 MCAP 压缩参数。

## 测试与验收

### 无硬件验收

- Runtime 与测试不需要安装 ARX5 或 RealSense SDK。
- 十条连续 Episode 均能独立开始、停止和提交。
- 正式 Episode 恰好包含 MCAP 与 JSON，不生成 SHA。
- 必需 Stream 停止后生成可读取的 `aborted` Episode。
- Backend 关闭或目录提交失败时只保留 `.partial`。
- 低频只写警告，不改变 `success`。
- 空闲时 `Ctrl+C` 正常退出；录制时 `Ctrl+C` 尝试生成 `aborted`。
- 遗留 `.partial` 会被报告，但不会被修改。

完成以上条件后，计划状态可更新为 `implemented`。

### 真机验收

- 使用真实 `/sensors/...` 与 `/embodiments/...` Topic 完成 90～150 秒 Episode。
- Runtime 无 Vendor SDK import，替换 Source 不修改状态机与 Store。
- 每个 Stream 的 `expected_hz`、`message_count`、`observed_hz` 和 `max_gap_ms` 写入 metadata。
- 重复 Episode 无后台进程、文件句柄或临时目录泄漏。

真机验收通过后，计划状态才可更新为 `verified`。

## 部署与回滚

- Container 增加独立可写 Episode Volume，不把数据写入只读源码挂载。
- 继续使用 Privileged Docker 与 Host Network，本计划不收敛权限。
- 更新失败时回退上一 Docker Image；不得删除已生成 Episode 或 `.partial`。
- `plans/last_edit.md` 只在实施或部署发生后更新，本次计划落盘不更新。

## 开放决策

- `min_free_bytes` 默认值：等待三路 720p 吞吐基准。
- 三颗 D405 的物理角色映射：逻辑 Topic 已冻结，正式 Station 配置等待现场确认 left/right/overview。
- 生产组合入口：Port 已一致，后续只增加 Adapter factory、固定 Stream 配置与 CLI launcher，不修改控制面核心或 ROS Adapter。

这些开放项已由 Hook 或等待边界隔离，不阻塞 Episode Runtime 核心开发。

## 验收结果

- 2026-08-15：Episode Core Models 已完成并通过单元测试与独立安装链路验收。
- 已冻结 `EpisodeState`、`EpisodeOutcome`、`StreamSpec`、`EpisodeRequest`、`StreamMetrics` 与 `EpisodeResult`；未引入 ROS2 或 Vendor SDK 依赖。
- 2026-08-15：Episode Ports 已完成并通过全仓 13 个单测与独立安装链路验收。
- 已冻结同步轮询式 `RecordTrigger`、`RecordingBackend` 与 `StreamMonitor`；toggle、必需流轮询和 aborted 干净关包语义已确认。
- 2026-08-16：Metadata v1 Contract 已完成并通过 Draft 2020-12 Schema 检查、全仓 19 个单测与工作区外链路验收。
- 已冻结单调时长、逐 Stream 统计、v0.1 标定空桩和严格扩展边界；生产 Runtime 未增加 Schema 验证依赖。
- 2026-08-16：Metadata Writer 已完成并通过全仓 25 个单测与独立安装链路验收，生成结果通过 Metadata v1 Schema。
- 2026-08-16：Episode Store 已完成并通过全仓 31 个单测与独立安装文件生命周期验收；失败路径保留 partial，成功目录严格为两文件。
- 2026-08-16：Episode Runtime 状态机已完成并通过全仓 36 个单测与独立安装连续十 Episode 链路验收；单调时长及 success/aborted/finalization 路径均覆盖。
- 2026-08-16：Keyboard Trigger 已完成并通过全仓 40 个单测与独立安装 PTY 链路验收；终端 context 退出可恢复设置。
- 2026-08-16：CLI Core 已完成并通过全仓 43 个单测与独立安装两 Episode 链路验收；真实 launcher 明确等待 main Adapter 工厂。
- Episode Runtime 开发分支职责内的状态机、Hook、Store、metadata、Keyboard 与 CLI Core 已实现并逐模块验收。
- 2026-08-16：同步最新 main 后完成全仓 51 个单测与 8 条安装后链路回归；低频 warning 与目录提交失败路径补充通过。
- 2026-08-16：main 已完成真实 ROS Adapter。Jazzy 容器内连续 10 次录制均生成单一可读 MCAP，50 Hz 数据审计无 warning；停止 telemetry 后 2.107 秒内返回必需流失败，异常 MCAP 正常关闭。
- Adapter 与冻结 Port 运行时一致，未修改 `src/arx5_collection/episode/`。
- 计划状态保持 `implemented`；`verified` 等待生产组合入口、Docker 整链与真机 90～150 秒验收。
