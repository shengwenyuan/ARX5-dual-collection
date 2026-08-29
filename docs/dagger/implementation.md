# DAgger 实施记录

- Status: `D0 accepted; D1b v2 Take-over and unified Timeline accepted`
- Updated: 2026-08-29
- Branch: `main`

## 当前迭代

当前目标是以统一 C++ D405 Source 消除高带宽二次订阅，同时保持可被 Take-over 复用的最小 D0 数据路径：

```text
librealsense 三 Pipeline -> 六路 ROS Topic / MCAP
  -> 进程内 Color Matcher + ROS ArmState
  -> Unix socket request + 双缓冲共享内存
  -> Python LocalVlaSnapshotClient
  -> AsyncPi05PolicyClient
  -> PI-style Policy Server
  -> Session JSONL diagnostics
```

本轮明确撤回：

- C++ `Pi05ObservationAdapter` 与 `GetPi05Observation` service。
- 逐次 `policy_inference`、`policy_action` 及 observation source stamp 的 MCAP 记录。
- Shadow 对 command、Gateway、control authority 或 Episode outcome 的影响。
- 把模型协议、Policy、Recorder 或控制逻辑塞入相机 Source。
- 通过DDS Service传输Snapshot请求、响应或图像payload。

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

## 2026-08-19 D1b Gateway 候选实现

- 新增模型无关的 `PolicyActionGateway`，独占 control epoch、command lease、一个在途 Policy Future 和有界 execution queue；旧 epoch 会同时关闭 gate、作废在途结果并清空 action。
- 新增 π0.5 joint action contract：Policy response 按 robot-space absolute `14D` 解释为左右各六关节 rad 与一个 normalized gripper，只消费配置的 execution horizon，再映射为左右各 `7D` Vendor command。
- contract 不插值、不裁剪、不静默 clamp；epoch、checkpoint、finite、joint step、joint departure 或 gripper bound 任一失败都会在 lease 打开前拒绝整个 ticket。
- `TakeoverController` 已改为真正的异步 readiness：Episode 初始和人工恢复都先停在 `RESUME_PENDING`，只有 Future 完成且 contract 通过后才产生 `POLICY_ACTIVE`。`NoActionGateway` 作为 immediate-ready 实现保持 D1a 已验收事件序列。
- 已冻结 v2 首版参数：robot-space absolute `14D`、`50/10/25 Hz`、joint step/departure `0.25/1.5 rad`、normalized gripper `[0,1]`、Policy wait `0.5 s`、command watchdog `0.12 s`。顺序执行 10 步后才请求 fresh observation；间隙不重复发布，只由 Vendor 保持最后 target。
- 新增 `FixedRateCommandExecutor` 与唯一 `RosDualArmControlPort`。paired publish 在 command lease 锁内完成；takeover 与旧 epoch Future/Publisher 线程并发时，gate close 返回后不会再产生旧 action。
- Vendor patch 将 `remote_slave` command callback 默认锁死；GO_HOME 和 gravity compensation 都再次锁死，只有 Gateway 完成 fresh action、安全检查和双臂 `enable_policy_control` 后才能开放物理控制。
- Episode 正常结束也执行 close gate、epoch++、clear action 和 G_COMP；任何 Take-over runtime fault 执行同一 fail-closed 路径、记录 `FAULT_HOLD`，并立即 finalizing 到 `dagger_fail/`。这是 Episode 级 failure，不阻塞长生命周期 Session；共享采集链路 failure 仍使用 `fail/ + SESSION BLOCKED`。
- w3 首版模型固定为 `stacking_five_paper_cups_pi05_v2/9999`，tree SHA-256 为 `6855485b55e04707d9c0aa96ad4ca1c8374afac5919d9f4777b71023ea7021a0`；宿主机路径不进入 profile，Compose 只挂载 checkpoint root。
- 本地全仓 `251 passed, 2 skipped`。w3 Collector 镜像 `sha256:691c9cbf462ef55cf8c3c0f8e53ec77ddf977588117d6aee80d68b369b7444f5` 已完成固定 ARX commit、Vendor patch 和六个 ROS package 编译；60 项 Container DAgger tests、CLI、ROS feedback/paired-command loopback 与重复 Publisher guard 通过。
- w3 完整 Policy 镜像重建受当前 PyPI DNS 阻断；在旧已验收 OpenPI 镜像上只覆盖本仓 Python 层得到临时候选 `arx5-dual-policy:dagger-d1b`，ID `sha256:d71c999f247f25a26ac9bbfd8fd67c51fcd1c7f88ef21a493c754f43a78f2ca8`。双容器均已静态确认同一 checkpoint、SHA 和 `50/14/10/25 Hz`，未加载模型。
- 2026-08-20 用户完成 w3 无模型 Vendor latch 验收：两个 command Topic 均为 `Publisher count: 0`；双臂 `enable_policy_control` 与随后 `gravity_compensation` 均返回 `success=True`。
- Controller 持久日志确认双臂启动时均处于 `Policy control is latched off`；测试未生成 Episode，Ctrl+C 后容器、slcand 和 CAN 均无残留。Vendor Controller 在完成 DisableMotor/CAN 回收后仍出现既有 SIGINT `-11`，与 D1a 相同，不影响锁存验收。
- D1b 无模型物理锁存闭环通过。下一步进入 v2 checkpoint 的模型 Take-over 单 Episode；首次动作前仍必须由用户启动并在真机旁监督。

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

## 2026-08-20 Episode / Timeline 时间锚点修复

- W3 v2 Take-over 已完成单 Session 双 Episode、多次人工接管与模型恢复，控制效果符合预期；原始 `/dagger/authority` 事件顺序和 metadata 区间结构正确。
- 离线分析发现 metadata 区间相对 Episode 起点存在 Episode 内固定偏移，且最后区间结束于停止清理完成时刻，而非右踏板触发时刻。
- Episode Runtime 现为唯一边界所有者：开始 Hook 接收 Recorder 前采样的单调时钟锚点；键盘/踏板 Trigger 在识别输入时携带事件时间；停止 Hook 直接接收该触发时刻。
- Authority Timeline 不再自行定义 Episode 起点。每条稀疏事件只采时一次，同一时间同时写入 `/dagger/authority` 和 metadata 区间边界；控制关闭、旧动作清空、重力补偿耗时不再延长最终区间。
- 本地全仓 `251 passed, 2 skipped`。W3 候选 Collector `arx5-dual-collection:dagger-timeline-anchor-20260820`，镜像 ID `sha256:024b54ded18e16ac97c28d4746ad7d92a9f81214779454bee10f3c96a2f8d487`，31 项容器内相关测试通过；未启动模型或设备。
- W3 验收 Episode `20260820T034131341693Z-5b0af467` 为 success，47.167 s、一次接管和恢复；八路频率稳定且无 warning/error。
- 五条 authority 事件分别对应初始模型、请求接管、人工生效、请求恢复和模型恢复。以事件单调时钟减 metadata 边界 offset 反推的五个 Episode anchor 全部为 `242645300406856 ns`，离散为 `0 ns`。
- 最终模型区间与 Episode duration 同为 `47.167449426 s`，停止边界误差为 `0 ns`。MCAP authority 接收时刻相对语义边界仅有 `0.056–0.155 ms` 正常发布延迟。
- Recorder 因先执行控制关闭和重力补偿，最后一条原始消息比踏板边界晚约 `9.813 ms`；该安全清理尾部不属于任何控制区间。原始 MCAP 保留，离线转换必须按下述事件时间契约过滤，不以文件物理末尾定义训练边界。
- 时间锚点修复收口，候选镜像通过验收。

## v3 Training-time RTC 迁移盘点

- W3 v3 checkpoint 为 `stacking_five_paper_cups_pi05_train_rtc_v3/9999`，类型明确为 `training_time_rtc`，tree SHA-256 为 `c5a2660f139fac5363ec95ec63da3f8c6b372098215ea64612539316fe5a70aa`。
- 运行基线仍为 `50 / 10 / 25 Hz`，新增 hard-prefix RTC：10 flow steps、最小执行 horizon 10、初始延迟估计 3 steps、10 次延迟历史。
- v3 参考 Client 直接提交 `640x480` RGB，而当前 Collector 的 π0.5 encoder 输出 `640x360`；迁移前必须按 v3 训练预处理冻结图像契约，不能仅因 Server 内部还能 resize 就视为等价。
- v3 不是替换 checkpoint 路径即可运行。现有 v2 Gateway、顺序执行器和 Policy envelope 必须升级为连续 action queue、重叠异步推理、delay 估计、带 control epoch/action sequence 的关联校验与原子 tail replacement。
- 相机 Source、Observation、MCAP、Authority Timeline、Take-over 状态机、Vendor latch 和 14D action contract 继续复用；RTC 诊断留在 Session JSONL，不增加普通推理 MCAP Topic。

## 2026-08-28 Snapshot 本地IPC收口

- 三路Canonical RGB-D继续以`848×480@30`可靠发布并完整录制；Snapshot只对同一真实因果组三路RGB做`INTER_AREA 640×360`缩放。
- 大图DDS Service会在完整Recorder负载下出现成簇长尾；即使图像payload移入共享内存，小型DDS response仍复现5/450和5/1000超时。
- 逐请求诊断确认超时请求的C++ callback均已进入并约3 ms完成，残余故障严格位于DDS response到Python future之间。
- 最终删除`GetVlaSnapshot` Service。`LocalVlaSnapshotClient`通过Unix socket发送单字节请求、接收32-byte结果，再从generation保护的双缓冲arena读取三图、来源时间戳和原始双臂状态。
- W3完整八路Recorder无动作验收：450/450@2.5 Hz、1000/1000@5 Hz、2000/2000@5 Hz；最长400.24秒，p99 4.15 ms、max 7.34 ms。
- 测试未加载Policy、未创建Action Publisher、未执行GO_HOME；临时Harness、概率日志和DDS消融参数均未进入主线。

## 2026-08-29 Shadow 与 Take-over 真机验收

- Shadow 连续完成一长两短共三条 success Episode，累计661次真实推理全部成功；Snapshot 长 Episode p99为4.52 ms、最大6.85 ms，未出现timeout、buffer未就绪或误终止。
- Take-over 首条 Episode 持续318.49秒，完成5次人工介入和5次模型恢复；417次推理提交中415次被当前epoch接受，未接受的2次随epoch切换或故障作废。控制队列最低4步，无underrun；命令间隔p99为42.71 ms、最大46.84 ms。
- 首条 Episode 最终仅因模型给出右夹爪归一化值`-0.002147`、违反冻结边界`[0,1]`而进入`FAULT_HOLD`，正确落入`dagger_fail/`并让Session返回READY。该故障与Snapshot、Policy通信无关。
- 同一Session第二条 Episode 持续68.11秒并success；96/96次推理接受，控制队列最低7步，无通信或调度故障。该条以人工控制区间结束，metadata边界闭合。
- 本轮确认本地IPC已覆盖真实Shadow和Take-over。夹爪边界容差及终端日志降噪作为独立后续修改，不回混Snapshot transport提交。

## 2026-08-29 夹爪输出边界与终端降噪

- 模型归一化夹爪输出的可接受范围冻结为`[-1,2]`；`[-1,0]`饱和为`0`，`[1,2]`饱和为`1`，区间外仍fail-closed。关节action继续禁止clamp或插值。
- RTC每次接受response后只写一条聚合`gripper_saturated` JSONL事件，记录数量、涉及侧和输入极值；不向终端逐action打印。
- Policy Server将`websockets.server`降至WARNING，健康检查不再反复打印`connection rejected (200 OK)`；健康检查本身保持启用。
- DAgger八路健康摘要从每2秒调整为每10秒；状态切换、故障、启动检查、RTC JSONL和Vendor文件日志均保留。
