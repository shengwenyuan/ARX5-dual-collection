# 生产编排与完整 Episode 集成计划

- Status: `w4-success-path-accepted-stability-pending`
- Parent: `meta_plan.md`
- Branch: `main`
- Target: `w3-arx5`、`w4-arx5`、ROS 2 Jazzy、单一 Privileged Docker Image

## 目标

提供唯一生产可执行入口 `arx5-collect`，用结构化 Python 工程代码完成设备核对、长生命周期采集 Session、八路就绪、双踏板/键盘 Episode、MCAP + JSON 原子提交和顺序关闭。临时 `/tmp` Shell 编排只作为实验依据，不进入生产实现。

## 当前基线

- Episode Models、Ports、Store、metadata、Runtime、双踏板/键盘 Trigger 和 CLI Core 均已进入 main。
- `RosbagRecordingBackend`、`RosStreamMonitor`、三路 D405 Source、双臂 Adapter、telemetry 与归位前置检查均已接入生产 Session。
- Station schema v2、统一 `station configure`/`devices`、真实 Runtime factory、进程管理、八路任务配置和 Docker 生产入口均已实现。
- W3 完成分模块与异常路径回归，W4 完成从零 Station 初始化和生产 success 链路验收。

## 冻结架构

### 单一入口

```text
arx5-collect devices --station-config ...
arx5-collect run --station-config ... --task-config ... --output-root ...
```

- 一个 Python 可执行程序承载所有生产子命令；不要求操作者启动多条 Shell 或多个窗口。
- 单一入口不等于单一类；CLI 只解析参数和展示结构化结果，不承载硬件步骤或 Shell 式流程。
- `devices` 同时核对 ARX5 与 RealSense 的逻辑角色、配置序列号、实测序列号、CAN/USB3 状态和匹配结果。
- `run` 构造真实 Store、Backend、Monitor、Trigger 与 Runtime，不允许 Fake Adapter 静默进入生产。

```text
arx5-collect
├─ SystemBringup
│  ├─ UsbfsManager
│  ├─ Usb2CanManager
│  └─ CanInterfaceManager
├─ RosProcessSupervisor
│  ├─ v2_collect
│  ├─ ArmState Adapter
│  └─ D405 Sources
└─ EpisodeRuntime
   ├─ Trigger
   ├─ Recorder / Monitor
   ├─ Store
   └─ metadata
```

### 进程所有权

生产编排器只使用显式 argv 启动子进程，不使用 `shell=True`，每个进程有名称、PID、日志、启动超时、健康状态和预期退出策略。

```text
preflight
  -> ARX5 official v2_collect
  -> ArmState Adapter
  -> D405 left -> real telemetry ready
  -> D405 right -> real telemetry ready
  -> D405 overview -> real telemetry ready
  -> Episode CLI loop
  -> cameras -> adapter -> ARX5 ordered shutdown
```

- 三颗 D405 必须逐台启动，并以真实 RGB/Depth telemetry 为就绪条件；Publisher 存在不代表设备出帧。
- 八个 Stream 在允许开始 Episode 前必须全部健康；运行期任一必需 Stream 停止，由 Runtime 关闭 MCAP 并提交 `aborted`。
- `arx5-collect run` 定义一个长生命周期采集 Session：usbfs、CAN 和 ROS Source 只启动一次，Episode 之间保持运行；Recorder 只在活动 Episode 内存在。
- 所有子进程进入编排器创建并持有的独立进程组；不得继承为不可控后台进程。
- `arx5-collect` 作为容器主进程运行，不通过 `docker exec` 附着到常驻空容器。

### Episode 与退出语义

- READY 时触发 activate，先完成双臂 GO_HOME 和恢复重力补偿，再启动 Recorder；录制时再次触发 activate，正常结束并提交 `success`，随后回到 READY。
- abort 触发器中止当前 Episode 并回到 READY；正式双踏板不可用时自动回退到键盘 `SPACE/A`。
- 录制时 `Ctrl+C` 关闭当前 MCAP、提交 `aborted`，随后退出生产程序并顺序停止 Source。
- 空闲时 `Ctrl+C` 直接顺序停止 Source；不创建空 Episode。
- 正式 Episode 目录只含 `episode.mcap` 与 `metadata.json`；失败提交保留 `.partial`，不覆盖、不自动删除。
- 低频只写 warning；必需 Stream 完全停止才触发 `aborted`。禁止插值、补帧和 SHA。

## 生产模块

```text
src/arx5_collection/production/
  cli.py              # 唯一 console entry 与子命令
  config.py           # Station/Task 严格解析与八路契约
  devices.py          # ARX5 + RealSense 统一身份核对
  checks.py           # 统一 CheckResult、失败语义与阶段报告
  system.py           # usbfs、USB2CAN 与 CAN 生命周期
  processes.py        # RosProcessSupervisor 与受控进程组
  readiness.py        # ROS graph、类型与 telemetry 就绪屏障
  orchestrator.py     # Source 生命周期和 Runtime 装配
```

- Station 配置统一解析一次，Source、metadata 与设备核对共享同一模型，修复当前相机字符串/对象语义分叉。
- 提供冻结的八路 Task 配置；任务描述仍由采集任务文件决定，不写死在代码中。
- 自有 Adapter 的关闭必须无 traceback；Vendor Controller 若在完成内部关闭后返回异常码，记录为明确 Vendor shutdown warning，不伪装为运行期成功。

### 统一查验边界

查验使用统一结果协议，但判断逻辑保留在所属模块，禁止形成包含全部硬件逻辑的 God Class：

- Session 启动前：配置、SDK、设备序列号、USB3、可用磁盘。
- SystemBringup 后：usbfs、受管 slcand、CAN UP 与错误计数。
- ROS 启动后：子进程存活、固定 Topic 类型、八个真实 telemetry id。
- 每条 Episode 前：80 GiB 可用空间和当前八路健康状态。
- Episode 运行中：Source telemetry 与最终 MCAP 审计。
- Session 退出后：子进程、CAN、临时目录和文件句柄回收。

## 分步实施

### 1. 配置与设备身份

1. 冻结 Station v2 解析模型和八路 Task 配置。
2. 实现统一 `devices` 命令，并覆盖缺失、重复、错配、非 USB3 与 CAN 未就绪。
3. metadata 改用统一模型，保证序列号正确写入 JSON。

### 2. 生产进程与就绪屏障

1. 实现 `RosProcessSupervisor` 与受控进程组，覆盖启动失败、意外退出、INT/TERM 顺序关闭和日志定位。
2. 实现 ARX5、Adapter 与三颗 D405 的结构化命令定义；D405 逐台启动。
3. 以八个 telemetry stream id、固定 Topic 类型和进程存活共同判定就绪。
4. 修复 ArmState Adapter 的 ROS context 关闭异常。

### 3. 完整 Episode 接线

1. 实现真实 Runtime factory：`EpisodeStore + RosbagRecordingBackend + RosStreamMonitor`。
2. 接入双踏板/键盘开始结束、连续 Episode、`Ctrl+C` aborted 后退出和遗留 `.partial` 报告。
3. 生成且校验恰好两个文件；metadata 指标必须来自最终 MCAP。

### 4. Docker 生产部署

1. Image 安装 `arx5-collect` console entry；SDK 自检追加生产入口和配置检查。
2. Production Compose 以 `arx5-collect run` 为容器主进程，并通过一个附带 TTY 的 Compose 命令启动；不使用 `docker exec`，不复制临时编排脚本。
3. 继续使用 privileged + host network + 1 GiB SHM；设备权限收敛不进入 v0.1。

## 测试与验收

### 无硬件

- 配置、身份匹配、进程生命周期、就绪屏障与信号路径单元测试。
- Fake 子进程覆盖启动失败、中途退出、关闭超时与清理顺序。
- Fake ROS Source 覆盖八路就绪、缺一路、低频和停流。
- 连续十条 Episode、success、aborted、Ctrl+C、提交失败与 `.partial` 保留。

### w3 / w4 真机

1. `devices` 一条命令完成双臂、三相机和双踏板七个设备逻辑身份与链路核对。
2. 单一生产入口启动全部 Source；未满足八路真实出帧时禁止录制。
3. 短 Episode 验证 `episode.mcap + metadata.json`、八路指标和顺序关闭。
4. 90～150 秒正式 Episode 验证六路 30 Hz、双臂原生频率、无持续资源泄漏。
5. 真机注入一个必需 Source 停止，验收 `aborted`、可读 MCAP 与完整 JSON。

## 里程碑完成条件

- 操作者只使用一个生产入口，不再维护多窗口 Shell 编排。
- 正常与异常 Episode 均满足两文件、原子提交和真实 MCAP 指标契约。
- 项目自有进程均可无 traceback 顺序关闭；无 Source、Recorder、临时目录或文件句柄泄漏。
- 150 秒八路 Episode 与必需流停止测试通过后，本计划状态更新为 `verified`。

## 实施记录

- 2026-08-16：完成 Station/八路 Task 严格模型、统一 CheckResult、usbfs/USB2CAN/CAN 生命周期和独立进程组；全量无硬件测试通过。
- 2026-08-16：完成统一 devices 核对、常驻 telemetry 就绪门、逐颗 D405 放行、真实 Runtime 装配、SPACE/SPACE 与 Ctrl+C 语义，以及 Production Docker target/Compose；全量测试为 107 passed、16 subtests passed。
- 2026-08-16：production image、PID 1、独立进程组、五设备、八路 READY、连续两条短 Episode 和完整回收已在 w3 通过；详见 `docs/milestones/production-session.md`。
- 2026-08-16：重复 Ctrl+C 清理问题已修复并以连续双 INT、`EXIT=0` 真机复验；全量无硬件测试更新为 109 passed、16 subtests passed。
- 2026-08-17：W4 从标准 Docker Engine 和空 Station 配置完成部署，七设备身份复核全部匹配；多次生产启动成功，两条代表性 success Episode 的八路 MCAP、metadata、频率、单机 RGB-D 配对和统一退出均通过，详见 `docs/milestones/w4-production-session.md`。
- success 主路径已允许批量采集。待验收：90～150 秒正式八路 Episode、必需 Source 停止后的 aborted 链路和长期压力；完成前不标记整体 `verified`。

## 已对齐决策

- `arx5-collect` 全权管理 usbfs、USB2CAN、CAN 和 ROS 子进程；内部保持清晰模块边界。
- 一次 `run` 是一个长生命周期采集 Session，硬件不会在 Episode 之间重启。
- 每条 Episode 默认要求至少 80 GiB 可用空间。
- activate 是正常开始/结束信号，abort 中止当前 Episode 并继续 Session；双踏板缺失时分别回退到 `SPACE/A`，录制中 `Ctrl+C` 提交 `aborted` 后退出整个 Session。
