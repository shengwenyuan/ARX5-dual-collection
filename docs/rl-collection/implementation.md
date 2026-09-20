# infer collection 实现与验收记录

2026-09-20。状态：**collection 代码和离线测试完成，停在真机测试前。** 未操作机械臂、CAN、相机或模型服务器，未创建完成 commit，也未更新上层 submodule gitlink。

## 实现范围

- `arx5-collect infer` / `scripts/arx5 infer`：固定已有 `training_time_rtc` checkpoint；其他策略类型启动前拒绝。
- `READY` 新右踏板 → 既有检查和 HOME → Recorder 启动、RTC bootstrap → `INFER RUNNING`。运行中右踏板 success、左踏板普通任务 fail；后者 `errors=[]`，无接管、无专家片段。
- 结束先关闭已有调度器的 gate、废弃 pending 推理并恢复重力补偿，保留默认 0.1 秒传感器采样机会，再停止 Recorder、提交文件。尾部采样机会不代表已验证末帧可用。
- 提交后等待新右踏板，不自动 HOME 或自动开始。READY 的左踏板及同批双踏板冲突不会启动；运行中同批双踏板冲突记录 aborted；Ctrl+C 中断录制则 aborted 并退出。
- infer 使用已有 station 的 `activate`（右）、`abort`（左）设备绑定，独立映射模式语义。必须接齐踏板，不静默回退键盘。原有 demonstration/DAgger 的按键和回退逻辑保持原样。
- 复用 `DaggerSessionBuilder`、`open_takeover_action_runtime`、RTC scheduler、Snapshot、Recorder 和 EpisodeStore。没有第二套 scheduler、MCAP 审计器、exporter 或训练采样实现。
- Recorder 原本就等待包含 additional topics 在内的订阅就绪；infer 直接复用。原有 RTC 日志继续保留，没有增加 request/chunk 全链路审计。

## 输出契约 v1

每个已提交 episode 仍只有 `episode.mcap`、`metadata.json`。成功在 output root 直属 episode 目录；任务失败及运行故障在 `infer_fail/<episode_id>`，abort 在 `abort/<episode_id>`。**目录不能代替质量判定：普通 fail 必须保留。** `.partial` 不属于已提交输入。

唯一新增话题：`/infer/command`，类型 `arx5_collection_interfaces/msg/InferCommand`。

| 字段 | 定义 |
| --- | --- |
| `episode_id` | 与 metadata 顶层 ID 一致 |
| `step_index` | 每局从 0 开始；每次双臂 publish 正常返回后记录一次 |
| `header.stamp` | 调用双臂 publish 前采样的主机 ROS system time；不是硬件 ACK 时间 |
| `monotonic_time_ns` | 同一次发送边界采样的主机单调时间；与 header 构成时钟对应点 |
| `action[14]` | 左 6 关节、左夹爪、右 6 关节、右夹爪；关节 rad，夹爪为 `arx5-gripper-v1` 原始执行器坐标 |

动作是现有 action contract 变换、限幅后的实际发送参数，不是 measured state，也不是原始模型输出或未执行的 action chunk。ROS publisher 正常返回不代表硬件到位。命令发送/记录发布异常走已有故障停止路径；不构造左右臂 ACK 事务。

传感器仍使用既有头时间戳；现有相机使用 RealSense Global Time。命令禁用 ROS simulated time。exporter 负责比较时间、对齐与连续性判定，不能把此对齐声称为精确复原 VLA 请求图像。

`metadata.collection_type="infer"`，无 `dagger` 对象；`extensions.infer` 包括：

- 起止、标签、最后实际动作、动作停止、请求关闭录制的单调时间。顶层 `timing` 保持现有 UTC 时间格式。
- `termination_reason`：`human_label | label_conflict | operator_abort | runtime_fault`。`label_source`：右/左/双踏板或 null；故障优先于同时间到达的普通结果。
- `command_count`：成功返回的双臂发送调用次数，即使后续记录发布失败，也保留该已发送计数；故障样本交 exporter 隔离。
- checkpoint SHA、三份输入配置 SHA、软件版本、任务 prompt、动作维序/单位、夹爪校准及 offset、checkpoint profile、控制参数和 RTC rollout 配置。归一化身份沿用已校验的 checkpoint tree SHA 所对应的 server assets，不另建归一化配置。
- `recording_completed=true` 只在 Recorder stop 与现有 finalizer 正常返回后写入。`commit_protocol=episode_store_atomic_rename` 描述提交协议，**不伪造提前完成的 committed 标志**；是否提交以目录原子改名成功为准。

infer CLI 默认复用 `--no-compress` 路径，不做结束后 MCAP 重写压缩。原有 backend/finalizer 检查仍保留。exporter 的动作对齐、fail 接纳、整局隔离及 LeRobot 输出仍是下一模块工作；此输出没有宣称已满足全部阶段 B EXPO replay 输入。

## 已执行的离线验收

基线：366 passed、1 skipped。实现后的全量结果见下方最终记录；新增测试位于 `tests/infer/`。

- 假设备 episode：右 success、左 fail、冲突 aborted；schema 合法；结果目录与 errors 正确。
- 多局流程：每局 HOME 一次，左踏板不能启动下一局，步号重置；HOME/bootstrap 缓存输入沿用 drain/disarm 清除。
- 真实 RTC 调度器 + 假控制端：记录最终 14D 下发值（包括夹爪 offset/转换），停止后迟到推理不继续输出，新局重新 bootstrap。
- 故障：策略错误、传感器故障、发送/记录发布错误、Ctrl+C、停止保护失败、Recorder stop/commit 失败。失败的 finalization 保留 `.partial`，不宣布正常完成或自动下一局。
- 并发边界：记录发布失败即使撞上结果标注，也不能变成正常 success/fail。
- 入口和资源生命周期：infer 接线、退出后 executor/policy 关闭，缺少踏板不启动 Session；旧模式全量回归。

假 backend 的测试文件不是可解析 MCAP；ROS 消息测试使用假消息对象。**这些测试不代表 ROS 编译、真实录制或 exporter 接入已完成。** 当前 Mac 无 `rclpy`/ROS，Docker daemon 未运行；不在此环境伪报该部分通过。

## 下一步：先验证运行环境，再共同做真机

以下命令只作交接，本轮未执行 build / run。复用工作站已有 `/var/lib/arx5-collection/dagger.env`、station 与 RTC policy TOML；确认 checkpoint SHA、任务描述及 output root 对应本次实验。新增 ROS 消息需要重建 collector 镜像，不能仅替换 Python 文件沿用旧消息包。

在 Linux 工作站仓库内，仅构建 collector（不启动硬件）：

```bash
docker compose --env-file /var/lib/arx5-collection/dagger.env \
  -f docker/compose.dagger.yaml -f docker/compose.infer.yaml build collector
```

可在不挂载设备的容器中先确认消息导入与 CLI：

```bash
docker run --rm --entrypoint /ros_entrypoint.sh arx5-dual-collection:dagger \
  python3 -c 'from arx5_collection_interfaces.msg import InferCommand; print(InferCommand())'
docker run --rm --entrypoint /ros_entrypoint.sh arx5-dual-collection:dagger \
  arx5-collect infer --help
```

若 `dagger.env` 设置了自定义 collector image，上面两条命令需使用该实际 image tag。再做无机械臂的 ROS publisher/Recorder 小样本验证，确认新话题能写入真实 MCAP、时间戳及动作可回读；这项目前待完成。

与用户共同确认现场准备后，沿用原有环境变量，启动：

```bash
scripts/arx5 infer --rgb-only
```

去掉 `--rgb-only` 则使用既有完整传感器配置。宿主机入口复用已有 DAgger compose 服务、镜像与挂载；输出路径仍须位于现有容器挂载范围内。

| 真机验收项 | 预期 | 当前 |
| --- | --- | --- |
| 右踏板开始 | HOME 后才进入任务录制/推理；HOME 无 command 记录 | 待执行 |
| 右 success / 左 fail | 正常提交，各自 outcome 正确，普通 fail 的 errors 为空 | 待执行 |
| 连续至少三局 | 落盘后静止等待；新右踏板才下一次 HOME；步号从 0 重启 | 待执行 |
| 长按/释放/再次踩下 | HOME、bootstrap、finalization 中按键不得成为后续自动开始/结束 | 待执行，必须验证 |
| 双踏板冲突与中断 | 同批输入冲突 aborted；Ctrl+C 走停止保护；不伪造任务标签 | 待执行 |
| 实际 MCAP 交接 | 新话题、计数、传感器尾帧存在；由 exporter 验证可用性 | 待执行 |

现有 HID 协议实现只识别已知 PRESS report，没有经验证的 RELEASE 编码。当前复用 drain 和 debounce，**不能仅凭离线测试保证所有固件的长按行为**。现场先验证输入报告；若长按会连续重发，必须依据实测报告补齐释放门控并重新测试，通过前不验收锁定 commit。

## 最终离线结果

在真实工作目录 `/Users/shengwenyuan/1011/ARX5-dual-collection` 执行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests \
  -p no:cacheprovider --basetemp=/tmp/infer-collection-final-test
```

结果：**394 passed、1 skipped，14.30 秒**；相对基线新增 28 项 infer 测试。跳过项在基线中也存在。

`git diff --check`、改动 Python 文件 AST 语法检查、两个 infer CLI help、Compose 合并配置验证均通过；Compose 检查未启动容器。按 Python/宿主机入口源码增删口径，新增 infer 包 477 行，共享代码新增 62 行、删除 9 行，合计约 **548 行**；不含测试、schema、消息声明/构建配置与文档。低于原 600–1,100 行设计预算，未为达到行数增加功能。

本结果只验收 collection 的离线逻辑。ROS 消息生成与实际录制、踏板释放/长按、真机多局，以及 exporter/replay 接入仍按上表保持待验收。
