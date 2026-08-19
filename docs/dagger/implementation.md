# DAgger 实施记录

- Status: `D0 Shadow accepted; D1a Take-over no-action single Episode accepted`
- Updated: 2026-08-19
- Branch: `main`

## 当前迭代

当前目标是以统一 C++ D405 Source 消除高带宽二次订阅，同时保持可被 Take-over 复用的最小 D0 数据路径：

```text
librealsense 三 Pipeline -> 六路 ROS Topic / MCAP
  -> 进程内 Color Matcher + ROS ArmState
  -> /dagger/get_snapshot
  -> Python RosVlaSnapshotClient
  -> AsyncPi05PolicyClient
  -> PI-style Policy Server
  -> Session JSONL diagnostics
```

本轮明确撤回：

- C++ `Pi05ObservationAdapter` 与 `GetPi05Observation` service。
- 逐次 `policy_inference`、`policy_action` 及 observation source stamp 的 MCAP 记录。
- Shadow 对 command、Gateway、control authority 或 Episode outcome 的影响。
- 把模型协议、Policy、Recorder 或控制逻辑塞入相机 Source。

本轮保留：

- 两 Container 的 Policy Server / Collector 部署边界。
- checkpoint SHA-256 对齐。
- PI 风格三相机、双臂 state、prompt request。
- Shadow 失败隔离和 metadata 汇总。
- 双踏板 DAgger profile；Shadow 忽略左踏板。
- `/dagger/authority` 接口契约，供 D1 Take-over 使用。

## 实施步骤

1. 重写需求，冻结数据与进程边界。
2. 删除旧 C++ Adapter、ROS service 和高频 Policy MCAP schema。
3. 实现小型有界缓存与纯函数因果选择。
4. 实现 Python ROS Sampler；callback 只入缓存，worker 负责转换。
5. 将 Policy Client 改为单 worker 的异步 submit/Future 接口，支持 epoch 作废。
6. Shadow 改为单在途调度，将逐次结果写入 Session JSONL。
7. 完成全仓测试、ROS interface 构建和 Docker 镜像构建。
8. 在 W3 仅部署镜像；由用户启动 45–60 秒 Shadow 真机验收。

## 已知历史与结论

旧 C++ Adapter 曾在合成测试和 MCAP 回放中通过，但真机与 Recorder/Policy 并发时缓存相机消息落后 0.2–2.1 秒，而同一时刻 MCAP 原始帧年龄仅 0.35–32 ms。多轮 callback group、QoS 和唤醒修正没有稳定消除该差异。

这证明生产 D405/USB/原始 Topic 正常，也证明旧 Adapter 的调度结构不适合作为 Take-over 基础。本轮不继续在该实现上打补丁。

旧 Shadow 已验证的故障语义继续有效：observation/policy 失败不得伪造 action，不得终止 Episode；恢复后可继续诊断。旧 Shadow 的成功 inference/action MCAP 对照不再属于新数据契约。

## D0 验收条件

- 单元测试覆盖三相机严格配组、因果 ArmState、freshness、无完整组失败和 YUYV 转换。
- Async Client 保证单 worker、关联校验、旧 epoch 响应作废和有界关闭。
- Shadow 逐次诊断只写 JSONL，metadata 汇总一致。
- MCAP 不再包含普通 inference、action 或模型输入副本。
- W3 90–150 秒内八路流保持既有频率；Sampler 不出现“原始 Topic 新鲜但本地 cache 秒级滞后”。
- 左踏板无影响，右踏板正常结束，Episode outcome 与 Shadow 故障解耦。

## D1 入口条件

D0 真机验收回写本文件前，不实现或运行 Take-over。D1 开始前还必须冻结当前 checkpoint 的 action 语义、控制频率、执行 horizon 和 Gateway 安全阈值。

## D1a Take-over 无动作候选

D1a 先验证控制权状态机、稀疏事件和 metadata，不连接 Policy Server，不创建 action publisher，也不执行模型 action。独立入口为 `arx5-collect dagger takeover-dry-run`；CLI 仅负责参数适配，组装逻辑位于 DAgger Application，权限逻辑位于独立 Take-over Controller。

- 右踏板只负责 Episode 开始/成功结束；左踏板只负责控制权交替。键盘 fallback 为 `Space / T / A`。
- 状态顺序固定为 `MODEL_CONTROL -> HANDOVER_PENDING -> HUMAN_ACTIVE -> RESUME_PENDING -> MODEL_CONTROL`；任何副作用失败进入不可自动恢复的 `FAULT_HOLD`。
- 接管顺序固定为先关闭 Gateway、递增 control epoch、清空旧动作，再请求双臂重力补偿；恢复顺序必须先完成 Gateway readiness，才能产生 `POLICY_ACTIVE`。
- `/dagger/authority` 只记录控制权边界；专家区间严格定义为 `HUMAN_ACTIVE -> RESUME_REQUESTED`，pending/fault 空隙不伪装为模型或人工控制。
- metadata 继续只保存 DAgger 类型、checkpoint SHA-256、介入次数和不重叠控制区间。
- D1a 使用 `NoActionGateway`，因此日志中的 `MODEL_CONTROL/POLICY_ACTIVE` 只是权限状态机状态，不代表存在模型动作输出。
- DAgger Arm profile 原子选择 `v2_joint_control`、`/arm_slave_*_status`、Canonical ArmState Adapter 以及 slave reset/gravity service；启动时额外拒绝已有 `/arm_master_*_status` publisher，防止 dry-run 与动作进程并存。

D1a 的本地纯逻辑与 Application 组装测试已通过。W3 无硬件集成确认 authority 消息字段完整往返，动作 Publisher guard 能 fail closed，真实 rosbag2/MCAP 能同时录制主流与 `/dagger/authority`。还需完成单次交替和多次交替真机验收；验收前不实现真实 Gateway。

候选镜像为 `arx5-dual-collection:dagger-d1a-20260819`。本地全仓 `233 passed, 2 skipped`；W3 Container 的 42 项 DAgger 与 10 项 profile/orchestrator/trigger/reset 测试通过。该镜像未连接 Policy Server，也未发布机械臂动作。

首次真机启动在 Episode 前置 GO_HOME 阶段停止。服务在 10 ms 内返回 accepted，但双臂没有到达 Vendor home；确认 CAN、Canonical telemetry 和服务发现均正常。根因是官方固定版本 `c783287` 的 `v2_collect.yaml` 配置了 Vendor home，而 `v2_joint_control.yaml` 的两个 slave 节点没有 `go_home_position`。

修复使用独立、可审计的 `docker/vendor/v2_joint_control.yaml` 覆盖 Vendor DAgger 配置，左右 slave 与 teaching profile 使用同一组 home 参数；C++ 服务补丁保持不变，不修改 reset 时序、收敛阈值或超时。候选镜像 `arx5-dual-collection:dagger-d1a-homefix-20260819` 已通过 SDK 安装配置检查和 18 项 D1a/profile/reset 容器回归，镜像 ID 为 `sha256:d7100ee4efc77a9f062c3ca862599ff947f501a05078dd9289d0cbec7900f82e`。

## 2026-08-19 D1a 单 Episode 验收

- Episode `20260819T141622554095Z-4b29f83f` 正常 success；前置 GO_HOME 通过真实收敛检查，证明 slave Vendor home 修复有效。
- 70.53 秒 MCAP 包含八路主线与 `/dagger/authority` 共九个 Topic、153767 条消息；双臂约 1000 Hz，三路 RGB-D 约 30 Hz，metadata 无 warning/error。
- authority 共五条，严格为 `POLICY_ACTIVE -> TAKEOVER_REQUESTED -> HUMAN_ACTIVE -> RESUME_REQUESTED -> POLICY_ACTIVE`；sequence 为 1–5，intervention 为 1，control epoch 从 0 递增到 1，无 `FAULT_HOLD`。
- metadata 控制区间为 model `0–14.456 s`、human `14.467–31.554 s`、model `31.555–70.482 s`；专家区间与稀疏事件一致，handover pending 未被伪装为人工或模型控制。
- 用户在第二条 Episode 前从 READY 退出；Vendor Controller 按既有行为在 SIGINT 后返回 `-11` warning。容器、slcand、CAN 均无残留，usbfs 恢复为 16 MB，不影响本轮验收。
- D1a 单 Episode 最小闭环通过。连续多 Episode 权限 epoch/sequence 真机验证按用户决定延期，不阻塞进入真实 Gateway 的独立设计阶段。

## 2026-08-19 D0 重构部署结果

- 已删除旧 C++ Adapter package、`GetPi05Observation` service、逐次 Policy MCAP message 和启动编排。
- 已实现 Python `RosVlaObservationSampler`：五个短 callback、三路小型图像历史、双臂有限历史、非阻塞严格因果选择。
- π0.5 编码与通用 Sampler 分离；YUYV→640×360 RGB 使用固定的 `opencv-python-headless 4.11.0.86`，不安装 Ubuntu GUI/GDAL/VTK OpenCV 依赖。
- 已实现单 worker `AsyncPi05PolicyClient`、epoch 作废和 Shadow 单在途调度。
- 普通 inference 不再增加 MCAP Topic；逐次结果写 `session-log/dagger-shadow.jsonl`，metadata 只写汇总。
- ROS interface 只增加 `AuthorityEvent.msg`，Topic 冻结为 `/dagger/authority`；D0 Shadow 没有控制权，因此不发布该事件。
- Mac 全仓：`222 passed, 3 skipped`；跳过项为本机缺少 ROS/OpenCV/真实 websocket 环境。
- W3 Collector Container：35 项 DAgger/ROS/真实 websocket codec 测试通过；五 Publisher ROS 集成测试通过。
- W3 首轮候选 Image：`arx5-dual-collection:dagger`，ID `sha256:f6b695ee5f1dc451f56579e436f235441ac548de4ec324566ce5cdef27d2adf1`，大小约 463 MB；已被下方配组修正版替代。
- Policy Server 代码与已部署 `arx5-dual-policy:dagger` 完全一致，本轮未重复构建。
- 旧 C++ Adapter Image 和 NumPy 转换候选均保留独立 retired tag，未删除，可用于诊断对照。

合成 720p/30 Hz + 双臂 1 kHz 分进程 smoke 已证明 Python Sampler 能在并发 ROS 数据面取到真实缓存并执行三路转换；但 Python 合成 Publisher 自身不能稳定维持真机数据频率，不能替代正式 W3 验收。当前严格停在 D0 真机启动前，不启动采集或控制设备。

## 2026-08-19 D0 配组故障结论与修正计划

- 本轮 W3 重启后的 Shadow 仅 8/902 次推理成功；八路原始 Topic 与 Policy transport 正常。
- 两份历史 MCAP 的三相机跨度分别主要位于 8.1 ms 和 12.1 ms，说明此前 16.7 ms 验收依赖偶然启动相位；双臂状态年龄最大约 1.48 ms，不是本轮主因。
- D405 不支持多机硬同步，因此 16.7 ms 不是可长期成立的设备契约。
- 将相机配组改为 overview 锚定的最近真实帧，最大跨度设为 40 ms；ArmState 2 ms 和 snapshot 100 ms 保持不变。
- 配组失败改为结构化分类，并在 JSONL 写实际值与门槛。补充任意 30 Hz 启动相位、丢帧和时间边界测试。
- 下一轮 W3 只从 `/home/lenovo/swy/ARX5-dual-collection-dagger-python-f6f42ad` 启动；旧部署目录暂不删除。

## 2026-08-19 D0 配组修正版部署结果

- `ObservationConstraints` 已改为显式配置：相机跨度 40 ms、ArmState 年龄 2 ms、snapshot 年龄 100 ms。
- JSONL 失败状态已细分为缓存未就绪、相机跨度超限、snapshot 陈旧、左臂陈旧和右臂陈旧，并写入实际值与门槛。
- Mac 全仓：`224 passed, 3 skipped`。
- W3 Container：37 项 DAgger 测试通过；独立 ROS 五 Publisher 集成测试通过。
- W3 修正版 Image：`arx5-dual-collection:dagger`，ID `sha256:0cfc3159c701f30a4792db6099f181502cd1854cf58cf1a411c30d4ff675387b`，大小约 463 MB。
- 代码与运行配置已部署到统一目录 `/home/lenovo/swy/ARX5-dual-collection-dagger-python-f6f42ad`；尚未启动真机采集或控制。

## 2026-08-19 D0 Python Sampler 真机结论

- Episode `20260819T093338042926Z-fdd1bd0d` 正常 success，49.03 秒八路数据完整；双臂约 1000 Hz、三路 RGB-D 约 30 Hz，无 MCAP 告警。
- MCAP 离线三相机跨度为 16.24–16.76 ms，40 ms 下 100% 可配组；双臂最大年龄约 1.22 ms。
- Shadow 143 次尝试全部失败：107 次 `snapshot_stale`、35 次 `camera_span_exceeded`、1 次 `left_arm_stale`。
- Python Sampler 缓存 snapshot 最高陈旧 1.74 秒、相机跨度最高 2.31 秒，确认高带宽 ROS callback 独立积压；阈值、D405、CAN、Recorder 和 Policy transport 不是根因。
- D0 不再继续调整阈值或修补 Python executor。按 `plans/dagger-cpp-snapshot-optimization.md` 实现窄职责、模型无关的 C++ Snapshot Source。

## 2026-08-19 D0 C++ Snapshot 部署结果

- 新增通用 `GetVlaSnapshot.srv` 和 `arx5_vla_snapshot` package；service 固定为 `/dagger/get_snapshot`。
- C++ Source 只订阅三路 Color 与双臂 ArmState；五路订阅和 service 使用六个独立 callback group，DDS depth 固定为 1。
- C++ 只保留少量真实历史并执行 40/2/100 ms 因果选择；返回原始 YUYV 与 ArmState，不含 π0.5、Policy、Recorder 或控制逻辑。
- Python `RosVlaSnapshotClient` 替代高带宽 Topic 订阅；YUYV→RGB、模型编码和异步推理边界不变。
- Mac 全仓：`225 passed, 3 skipped`。W3 C++ 4 项 gtest、Container 38 项 DAgger 测试及 C++/Python ROS 端到端测试通过。
- 对失败 Episode 的 48 秒 MCAP 原速回放中，含冷启动成功率为 139/146（95.2%）；最大相机跨度 24.24 ms，最大 ArmState 年龄 1.13 ms，未复现秒级 callback backlog。
- W3 正式镜像：`arx5-dual-collection:dagger`，ID `sha256:32a7997fe230754c72eb170e4f938682740c237b6b669f135a74646471c8b0ca`，大小约 464 MB。
- 代码、运行配置和镜像已部署到统一目录 `/home/lenovo/swy/ARX5-dual-collection-dagger-python-f6f42ad`；真机 Shadow 尚未启动。

## 2026-08-19 独立 C++ Snapshot 真机结论

- Episode `20260819T100405021883Z-2afab9f4` 正常 success，64.38 秒八路 MCAP 完整。
- Shadow 193 次请求仅 15 次成功（7.8%）；159 次 snapshot 陈旧、19 次相机跨度超限，末尾陈旧约 1.04 秒。
- 同一 MCAP 离线复算中，三相机 `<=40 ms` 为 1926/1927（99.95%），跨度中位数 10.32 ms；双臂年龄 99 分位均小于 1 ms。
- 结论：独立 C++ Subscriber 仍受 ROS/DDS 图像 fan-out 与调度积压影响。40/2/100 ms 标准不放宽，该生产结构被否决。

## 2026-08-19 统一 C++ D405 Source 部署结果

- 新增一个 `multi_d405_source` 进程，内部包含三条独立 librealsense Pipeline、采集线程、capacity-1 SDK queue、Global Time 校验和 Depth-to-Color align。
- 六路既有 Topic 继续独立发布；同一 Color message 同时进入进程内 Matcher，不再由 Snapshot 二次订阅图像。
- 普通生产关闭 Snapshot service；DAgger 复用 `/dagger/get_snapshot` 与现有 Python Client，Policy/PI/Recorder/控制边界不变。
- 本地回归 224 passed、3 skipped；W3 C++ 编译和 4 项 SnapshotBuffer gtest 通过。
- W3 候选镜像 `arx5-dual-collection:dagger-unified-d405` 已构建并部署为 compose 的 `arx5-dual-collection:dagger`，ID `sha256:e38b36f35ab7f12b798292721b1e76f3267754da439d5af04e324456e31bf367`；尚未启动真机采集或控制。

## 2026-08-19 统一 Source 720p 真机结论

- 第一条 46.03 秒 Episode 为 aborted；Shadow 125/138（90.6%）成功，最长连续失败 2.81 秒。右相机出现 1.14 秒 Header 断档和 12 个重复时间戳。
- 第二条 58.04 秒 Episode 为 success；原始 MCAP 的 40 ms 配组 100%，双臂最大年龄约 1.01 ms，但 Shadow 仅 116/174（66.7%）成功，最长连续失败 3.77 秒。
- 第二条 58 次失败全部是 `/dagger/get_snapshot` 超过 250 ms，而非 Source 因果选择失败。完整三张 720p YUYV service response 约 5.5 MB，成为当前数据面瓶颈。
- 当前生产分辨率按新增需求调整为 848x480@30，Color 与 aligned Depth 同时调整，理论负载为 720p 的 44.2%。先保持门槛和 service 实现复测；仍不通过才切换共享内存 transport。
- 848x480 候选镜像已通过 C++ 构建、37 项 DAgger Container 测试和 14 项生产/Station 契约测试，并部署为 `arx5-dual-collection:dagger`，ID `sha256:a25ef01d72b8f2001b4e45b2053f4c9a8d2ae2406edb425c0d521f0c31fca9f6`；尚未启动真机。
- 同镜像的普通采集入口已完成单 Episode 真机回归：39.01 秒八路 MCAP success，三相机 40 ms 离线配组通过率 100%，Snapshot service 关闭且 Session 资源完整回收。连续双 Episode 回归与 DAgger Shadow 仍待验收。

## 2026-08-19 D0 Shadow 848x480 验收结论

- Episode `20260819T121334470523Z-74ac0291` 正常 success，91.55 秒八路数据完整且 metadata 无 warning。
- 六路图像均为 848x480；三相机约 29.98–29.99 Hz，双臂约 999.87–999.95 Hz，同相机 Color/Depth 计数一致，无非单调 Header。
- Shadow 274/274 次 inference 成功，失败 0、恢复 0、最长连续失败 0；D0 的 95% 与 2 秒门槛通过。
- MCAP 三相机真实跨度中位数 14.36 ms、最大 31.19 ms，40 ms 下 100% 可配组；实际请求全部满足 40/2/100 ms。
- MCAP 为 13.411 GB / 12.49 GiB，折算 60 秒为 8.79 GB / 8.19 GiB。分辨率下降已消除本轮 service transport 瓶颈，共享内存方案不进入当前实现。
- D0 Shadow 收口。进入 D1 前仍须单独冻结 checkpoint action 语义、控制频率、执行 horizon 与 Gateway 安全阈值。

## 2026-08-19 Observation 实现收敛

- 删除已被统一 D405 Source 取代的独立 `vla_snapshot_source` Subscriber；`arx5_vla_snapshot` 仅保留被统一 Source 链接的 C++ 因果 Matcher。
- 删除无生产调用的 Python 因果配组实现；Python 只负责调用 `/dagger/get_snapshot`、模型字段编码和策略通信。
- 行为契约未变：Python 全量离线测试 220 passed、2 skipped；相关三个 ROS package 离线构建通过，C++ SnapshotBuffer 5 tests 全部通过。

## 2026-08-19 Application 与 ArmState profile 收敛

- `production/cli.py` 只保留参数适配；DAgger 资源由独立 Application Builder 组装，硬件与 ROS 生命周期由 DAgger Session Builder 组装。Take-over 不再向生产 CLI 堆叠逻辑。
- Canonical 输出固定为 `/embodiments/left_arm/state` 和 `/embodiments/right_arm/state`。普通示教 profile 启动 `v2_collect` 并读取 `/arm_master_*_status`；DAgger profile 启动 `v2_joint_control` 并读取 `/arm_slave_*_status`。Controller launch 与 Adapter 输入必须由同一个 profile 决定。
- action chunk size、action dimension、execution steps 和 control rate 由 DAgger TOML 配置。当前实验配置为 `50 / 14 / 10 / 25 Hz`，不把单一模型参数冻结在代码中。
- 本地 226 tests 通过、2 skipped；W3 独立镜像完成 6 个 ROS package 构建，35 个 DAgger tests 与 9 个 production profile/orchestrator tests 通过。Adapter 无硬件 smoke 明确打印 slave 输入到 canonical 输出的两条映射；尚待用户启动 ARX 链路完成真实 Topic 验收。
- 首轮真实 profile 测试发现 Session 仍固定启动 `v2_collect`，因此只有 master Topic，DAgger readiness 必然超时。profile 已扩展为同时选择 Controller launch 与 Adapter 输入；W3 重建后确认 DAgger 解析为 `v2_joint_control`、slave 输入和 25 Hz。
- W3 复测通过：`v2_joint_control` 下左右 Canonical ArmState 分别观测到 439/446 条消息，age 均为 18 ms；CAN 零错误，退出后 usbfs 从 256 MB 恢复为 16 MB。Arm profile 链路收口。
