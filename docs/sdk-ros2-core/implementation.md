# SDK 与 ROS 2 数据面实施计划

- Status: `validated`（当前生产数据面使用统一 C++ D405 Source、RGB8 Color、Z16 aligned Depth）
- Parent: `meta_plan.md`
- Branch: `main`
- Target: `w3-arx5`、ROS 2 Jazzy、单一 Privileged Docker Image

## 目标

在 `main` 建立只负责真实设备和 ROS 2 数据面的稳定核心：启动官方 SDK、发布固定逻辑 Topic、监督真实频率，并验证选定 Topic 能可靠写入 MCAP。

外层分支负责状态机、Hook、Store、metadata 和 CLI。两条开发线通过 ROS Topic、站点配置和已冻结的 Python Port 对接，不共享业务实现文件。

## 分支边界

`main` 实现：

- ARX5、RealSense SDK 构建、设备启动、容器与真机脚本。
- 三路 D405 ROS 2 Source，每路只输出彩色图、对齐 Depth 和原始时间语义。
- 双臂 ROS 2 Adapter，将 Vendor 七维数组拆分为六关节与独立夹爪字段。
- 固定 Topic、消息定义、站点设备映射与频率监督实现。
- `rosbag2_py + MCAP` 选定 Topic 录制技术验证；外层 Port 合并后实现其 ROS 2 Adapter。

外层分支实现：

- Episode 状态机、Trigger/Backend/Monitor Hook 定义。
- `.partial`、Store、metadata、原子提交与异常结果。
- 键盘、任务输入和 CLI。
- Fake Adapter 与无硬件测试。

`main` 不修改 `src/arx5_collection/episode/`；外层分支不修改 Vendor SDK、Docker bring-up、ROS 2 Source 和真机配置。合并前不得在两边复制同一功能。

## 冻结约束

- ARX5 只使用 `ARXroboticsX/ARX_X5:main`，排除 `ARX5_beta`。
- 双臂采用官方 `v2_collect`、`remote_master` 和重力补偿；采集侧不发布 `/arx_joy` 或运动指令。
- RealSense 使用稳定 `librealsense v2.54.2`；当前三路 848×480 RGB-D @ 30 Hz。
- v0.1 最初使用 `rclpy + pyrealsense2` 完成基线；当前生产编排使用一个 `rclcpp + librealsense` 进程统一拥有三颗 D405。
- 彩色图固定为设备 RGB8，ROS 编码为 `rgb8`；Depth 保持 `16UC1`。在线链路不保留 YUYV 分支，历史 YUYV MCAP 只在离线读取时兼容。
- 图像 Topic 使用 Reliable QoS；Fast DDS 使用 64 MiB SHM Segment、2048 消息队列，容器 `/dev/shm` 固定为 1 GiB。
- Depth 与同机彩色帧做空间对齐；禁止时间插值、补帧、重复帧和伪造同步帧。
- D405 不支持多机硬件同步。三台设备使用独立 Pipeline 与 Global Time 时间戳，不做跨设备 frameset 重组。
- 所有 Topic 使用逻辑名称，不暴露 CAN、USB 和序列号。
- v0.1 不发布 `CameraInfo`；设备身份与标定引用由 metadata 管理，跨相机外参标定留到 v0.2。
- 默认不做 SHA 校验；运行报告保存在 Git 忽略的 `reports/`。
- `usbfs_memory_mb` 至少为 `256`；容器启动时检查并按需临时设置，失败立即退出。

## 建议 Topic 契约

每颗相机：

```text
/sensors/camera_<left|right|overview>/color/image_raw
/sensors/camera_<left|right|overview>/aligned_depth/image_raw
```

每只机械臂的逻辑根暂定为：

```text
/embodiments/<left|right>_arm/state
```

相机沿用标准 `sensor_msgs/Image`。机械臂使用项目最小自定义 `ArmState`，保留 Vendor Header，并包含：`eef_xyzrpy[6]`、六关节位置/速度/原始电流，以及独立夹爪位置/速度/原始电流。Adapter 原样映射，不换算单位、不降采样。

## 已核查事实

- 官方 `v2_collect` 发布 `/arm_master_l_status` 与 `/arm_master_r_status`，类型为 `arx5_arm_msg/msg/RobotStatus`；消息包含 `end_pos[6]`、`joint_pos[7]`、`joint_vel[7]`、`joint_cur[7]`。
- 66.16 秒真机 MCAP 分别录得 66166 与 66165 条消息，两路实际频率约 1000 Hz；字段均为有限值，Header 最大间隔不超过 1.86 ms。
- 第七位位置、速度和电流随夹爪操作变化，确认必须从六关节数组中拆出。官方消息没有示教器输入字段，后者不进入本轮 `ArmState`。
- 历史 node010 MCAP 使用 `ARX5_beta` 直读后自定义的 `ArmStateRaw`，只保存六关节位置；相机只有压缩彩色图，没有 Depth，且 Topic 暴露序列号。它仅作为采样与 ROS 2 录制参考，不作为本项目消息契约。
- D405 官方规格明确不支持多机硬件同步信号。`librealsense v2.54.2` 的 `rs-multicam` 为每台设备启动独立 Pipeline；`syncer` 只能对送入它的不同 Stream 生成 coherent frameset，不构成官方三设备同步方案。

## 实施顺序

1. 实现 Python 单相机单进程 Source 骨架；Launch 启动三进程，按序列号映射逻辑位置并执行单机 Depth 对齐。
2. 对 YUYV 与 RGB8 各执行 30 秒 MCAP 测试，比较真实频率、CPU、写入吞吐和文件大小后冻结编码与压缩参数。
3. 建立最终 ROS 2 interfaces 与站点配置，加入消息契约测试。
4. 实现双臂 Adapter；只订阅官方状态并重新发布稳定逻辑消息。
5. 实现统一频率监督，报告帧数、平均频率、最大帧间隔和完全停流。
6. 在容器内验证 `rosbag2_py + rosbag2_storage_mcap` 对显式 Topic 的启动、停止、重复录制和干净关闭。已完成。
7. 外层 Port 合并后实现 `RecordingBackend` 与 `StreamMonitor` Adapter，不改变状态机或 Store。已完成，等待两树合并接线。
8. 合并两条开发线，生成单一部署镜像并执行 90～150 秒真机 Episode 验收。

## 主线保留内容

- 保留：ROS 2 Source、消息定义、Launch、站点配置、MCAP Adapter、频率监督、可重复真机脚本及测试。
- 保留：历史 YUYV/RGB8 A/B 结果摘要；当前基准工具只验证正式 RGB8 契约，动态 JSON 报告不进 Git。
- 不保留：一次性远端命令、临时 Topic dump、试验 MCAP 和为本轮操作临时生成的文件。
- 不移植：历史 node010 的 `ARX5_beta` 控制流程、序列号 Topic、RGB-only 数据契约和位置控制逻辑。

## 验收条件

- 容器重启后自动满足 USB 内存前提，三路 D405 和双臂均可见。
- 三路图像及对齐 Depth 连续 150 秒达到约 30 Hz，不伪造帧。
- 双臂稳定发布经真机确认的状态字段，至少覆盖六关节、夹爪、原始电流和 EEF；实际频率可测，不产生采集侧运动命令。
- Topic 名称、消息类型与站点映射固定且有测试覆盖。
- 显式 Topic 可连续开始和停止录制，生成可读取 MCAP，无残留录制进程。
- 接入外层 Runtime 时只新增 Adapter 和配置，不修改状态机、Store 与 CLI 核心。

## 部署与回滚

- 继续使用 privileged container + host network；不做设备权限收敛。
- 真机开发副本保持独立目录与容器名，不修改 Vendor 既有目录。
- 新镜像失败时回退上一镜像；不得删除 Episode、`.partial` 或本地测试报告。

## 开放决策

1. Vendor 字段的物理单位与 EEF 坐标系尚未由官方资料明确；v0.1 保留原始值和字段语义，不猜测换算。
2. 外层分支与 main 的合并顺序及真实 CLI factory 接线；两侧 Port 已一致，合并不得改写状态机或 Store。

## 验收结果

2026-08-15 完成双臂官方状态 MCAP 验收：

- 官方 `RobotStatus` 连续录制 66.16 秒，左 66166 条、右 66165 条，均约 1000 Hz。
- Header 最大间隔为 1.10 ms 和 1.85 ms；全部向量无非有限值，六关节、夹爪与 EEF 均有有效变化。
- 原始 MCAP 为 35.7 MiB，可由 Jazzy `ros2 bag info` 与字段分析工具读取。
- 测试完成后容器与 CAN 接口全部回收，无残留。
- 结论：采用最小自定义 `ArmState`，保留原始 Header 与数值并拆分夹爪；不纳入示教器输入，不主动降采样。

2026-08-16 完成 Python 三相机 ROS 2 数据链路验收：

- 三颗 D405 使用独立 `rclpy + pyrealsense2` 进程，发布六个固定 `sensor_msgs/msg/Image` Topic；单机 Depth 与 Color 计数一致。
- Best Effort 与默认 512 KiB Fast DDS SHM 会产生大量传输丢失；改用 Reliable QoS、64 MiB SHM Segment、2048 消息队列和 1 GiB `/dev/shm` 后丢失归零。
- YUYV 与 RGB8 的 12 秒 A/B 均达到 30 Hz；YUYV MCAP 为 3.6 GiB，RGB8 为 4.5 GiB，因此冻结 YUYV。
- YUYV 正式录制 151.66 秒，MCAP 为 46.9 GiB、共 27298 条；三路分别录得 4549/4549/4551 组对齐 RGB-D，平均 29.992～29.999 Hz。
- overview Header 最大间隔 33.344 ms；left/right 各出现一次约 66.67 ms 的真实间隔。未插值、补帧或伪造同步，rosbag2 未报告传输丢失。
- 测试序列号按临时顺序映射 Topic；真实 left/right/overview 物理映射尚未确认，不得写入正式 Station 配置。

2026-08-16 完成逻辑 ArmState Adapter 回放验收：

- 新增 `arx5_collection_interfaces/msg/ArmState` 与只读 Python Adapter；输入固定为官方双臂 `RobotStatus`，输出固定为 `/embodiments/left_arm/state` 与 `/embodiments/right_arm/state`。
- Adapter 只保留 Header、拆分六关节与夹爪并原样复制 EEF、位置、速度和原始电流；不换算单位、不降采样、不引用控制接口。
- 使用既有 66.16 秒官方 MCAP 以真实速率回放，逻辑 MCAP 左 66166 条、右 66165 条，共 132331 条；Header 频率均约 1000 Hz。
- 逐条直接比较确认 Header 与全部字段一致；不使用 SHA。Adapter 与 recorder 均干净退出，无残留进程。
- 最终验收报告位于 Git 忽略的 `reports/w3/2026-08-16/20260816-arm-state-adapter-replay-r5.analysis.json`。

2026-08-16 完成轻量频率遥测与 MCAP 统一审计验收：

- 新增固定 `/monitoring/stream_status` 与八个逻辑 stream id；相机 Source 和双臂 Adapter 在已有回调内统计 Header，不新增大图订阅或载荷复制。
- `StreamStatus` 每秒报告累计数、窗口数、窗口频率、最大间隔、最近 Header、停流时长与非单调计数；只提供事实，不在 Source 内判 Episode 结果。
- 双臂 66.16 秒回放中，遥测最终累计数与 MCAP 精确相等：左 66166、右 66165；窗口频率约 1000 Hz，无非单调 Header，映射字段仍逐条一致。
- 三相机遥测短测中，六个 Stream 的窗口频率为 29.307～30.178 Hz，最大 Header 间隔不超过 34.232 ms；同机 RGB/Depth 累计数一致，无非单调 Header。
- 统一 MCAP 审计工具直接读取八个数据 Topic 的 Header，输出与 Metadata `StreamMetrics` 对齐的 count、duration、observed_hz、max_gap 和 warnings；频率偏低只写 warning。
- Docker Hub 暂时无路由时，使用已验收镜像作为本地基底构建 ROS overlay 完成部署；正式 Dockerfile 与 SDK 版本未改变，网络恢复后仍执行标准全量构建。

2026-08-16 完成 ROS Runtime Adapter 验收：

- 新增 `RosbagRecordingBackend`，只录制 `StreamSpec.topic`；按 Jazzy 官方生命周期执行 `start_spin → record → stop → stop_spin`，并等待全部录制订阅建立后才允许 Episode 开始。
- rosbag2 自有隐藏目录严格收敛为单一 `episode.mcap`；异常结构保留现场，不覆盖目标，不生成 SHA。
- 新增只订阅 `/monitoring/stream_status` 的 `RosStreamMonitor`；必需流无遥测、心跳停止或数据静默会返回明确失败，低频只写 warning。
- w3 容器内连续启停 10 次，每包录得 30～31 条 50 Hz 测试消息，最终审计为 49.916～50.052 Hz，全部无 warning；11 个测试目录均仅含一个 MCAP，无 recorder 残留。
- 停止 telemetry 但保持数据继续发布时，2.107 秒后得到 `required stream smoke_arm_state telemetry stopped`；异常链路仍干净关闭为 171 条可读 MCAP。
- 全仓 50 个单元测试通过；正式 Dockerfile 已打包 Python Runtime Adapter，并由 SDK 自检验证 import。

截至 2026-08-16，数据 Source、逻辑 ArmState、轻量频率遥测与 ROS Runtime Adapter 已通过阶段验收；后续完整 Episode、统一 C++ Source 和 RGB8 结论见下文及对应专项文档。

2026-08-16 八路 ROS 2 数据面已完成 43.925 秒联合 MCAP 验收，结论见 `docs/milestones/eight-stream-mcap.md`。该结果标记短时联合录制里程碑，不替代 150 秒稳定性与完整 Episode 验收。

2026-08-24 完成 RGB8 生产链路验收：

- 在线 D405、Station 和 DAgger Observation 删除 YUYV 分支并统一为 RGB8；Depth Topic、Z16 编码、对齐、时间戳和频率标准不变。
- W3 单 Session 连续录制两条 success 和一条 aborted。两条 success 的三路 Color/Depth 分别各 1375 和 1060 帧，均约 29.992 Hz、逐帧同时间戳配对且无非单调 Header。
- 所有 Color 均为 `rgb8`、848x480、step 2544；所有 Depth 均为 `16UC1`、848x480、step 1696。RGB 样帧通道顺序正常。
- 三条 Episode 的跨相机配组覆盖率均为 100%，最大跨度不超过 6.51 ms；未调整 Reliable QoS 或任何因果门槛。
- 相机 Source 未在 Episode 间重启，Session 退出后资源全部回收。结论：RGB8 普通生产链路 PASS。
