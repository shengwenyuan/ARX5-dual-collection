# Operator UI beta2 真实逻辑集成计划

- Status: `alignment-required`
- Branch: `codex/operator-ui`
- v1 baseline: `2c9e5d4`
- Parent: `docs/operator-ui/implementation.md`

## 目标

保持 UI、硬件控制和 Episode 核心分层，用页面按钮调用现有能力，不在 JavaScript 内复制 Station、SystemBringup、Trigger、Recorder、Store 或数据清洗逻辑。

DAgger 是新的采集模式，动作与状态契约尚未定义。本阶段只在主操作区保留入口，明确显示 `NOT IMPLEMENTED`，不复用或伪装为当前示教录制流程。

## 建议拓扑

```text
Browser
  └─ Operator UI / JS Bridge（无特权）
       ├─ 静态页面
       ├─ 白名单 HTTP API
       └─ 状态与日志流
              │ Docker 内部网络
              ▼
     Collector Control（privileged，容器主进程）
       ├─ station configure 子进程 / PTY
       ├─ devices 子进程
       └─ arx5-collect run 受控进程组
              ├─ SystemBringup / ROS Sources
              ├─ QueueTrigger + PedalTrigger
              └─ EpisodeRuntime / Recorder / Store
```

- UI 容器继续无特权，不挂 Docker Socket，不访问 CAN、USB、udev 或 Station 配置。
- Collector Control 是唯一硬件权限边界，也是 collector 容器主进程；它用显式 argv 管理 CLI 子进程和完整进程组。
- 两个服务只通过 Compose 内部网络交换结构化命令、权威状态与日志；只有 UI 的 `127.0.0.1` 端口暴露到宿主机。
- 不通过解析普通 stdout 推断权威状态。Python 核心输出结构化 Session/Episode 事件；stdout/stderr 仅作为日志展示。
- UI 刷新后从 Collector Control 获取当前快照，不能把 Session 错误重置为 OFFLINE。

## Trigger 边界

现有 `AutoTriggerFactory` 在踏板可用时只返回 `PedalTrigger`，键盘只是整组回退。网页按钮不能依赖向 PTY 写入 SPACE/A。

beta2 增加统一组合 Trigger：

- 实体 activate 踏板与网页“开始/完成本条”产生同一种 `ACTIVATE` 事件。
- 实体 abort 踏板与网页“中止本条”产生同一种 `ABORT` 事件。
- 同一时刻只消费一个事件；重复输入必须去抖并按 Episode 状态拒绝。
- 踏板断连仍是运行错误，不能因为网页入口存在而静默降级。
- 组合层只负责事件汇合，不改变 `EpisodeRuntime` 的 success/aborted 语义。

## 按钮映射

| UI 入口 | 后端能力 | 首批状态 |
|---|---|---|
| Station 初始化 | `arx5-collect station configure` | PTY 交互，Session OFFLINE 时可用 |
| 设备检查 | `arx5-collect devices` | 结构化 JSON，Session OFFLINE 时可用 |
| 启动 Session | `arx5-collect run` | 受控进程组，返回真实 STARTING/READY/ERROR |
| 开始/完成本条 | 统一 Trigger `ACTIVATE` | READY/RECORDING 时可用 |
| 中止本条 | 统一 Trigger `ABORT` | RECORDING 时可用 |
| 退出 Session | 对 Session 发送有序停止 | 仅 READY，可等待完整 cleanup |
| 运行日志 | Collector 结构化事件与原始日志 | 只读 |
| Episode 列表 | Store 提交结果 | 只读，不扫描 `.partial` 伪装成功 |
| 数据检查 | 独立 `arx5-dataset` 流程 | 后续对齐，不与采集实时耦合 |
| Calibration | 尚未实现 | 保持打桩 |
| DAgger 模式 | 尚未定义 | 保持打桩，不启动采集 |

## 实施单位

1. 对齐控制拓扑、Task/输出目录来源和首批真实按钮范围。
2. 定义 Collector Control API、状态快照、事件 schema 和错误契约。
3. 实现组合 Trigger 及纯单元测试，不接触真机。
4. 实现 Collector Control 的 devices、Session、Episode 和日志控制。
5. JS Bridge 从 Mock Adapter 切换到 Real Adapter；保留显式开发 Mock 模式，但生产构建不得默认 Mock。
6. 接入只读 Episode 列表、磁盘信息和刷新恢复。
7. W3 依次验收 devices、Session READY、success、abort、退出与异常回收。
8. 真链路结论回写后再接入 Station PTY；真实 RGB 预览单独迭代。

## 验收边界

- 浏览器不能提交任意命令、路径或 shell 字符串。
- 任意 UI 重复点击不得启动第二个 collector 或 Recorder。
- UI 进程退出、刷新或断网不得终止正在进行的 Episode。
- 实体踏板和网页按钮均可控制同一 Session，且不会产生重复 Episode 边界。
- Collector Control 退出时按现有生产顺序回收 Recorder、ROS、CAN 与 usbfs。
- 真实结果只来自已提交的 `episode.mcap + metadata.json`。

## 待本轮对齐

1. 是否采用“无特权 UI + privileged Collector Control”双服务，不向 UI 暴露 Docker Socket。
2. beta2 首版的 Task config 与 output root 是由 Compose/启动参数固定，还是允许 UI 选择；建议首版固定，TODO 列表暂不下发真实 Task。
3. 是否先接入 devices、Session/Episode、日志，真机通过后再接 Station PTY；建议如此，避免同时调试交互终端和采集生命周期。
