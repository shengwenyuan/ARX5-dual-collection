# SDK 与 ROS 2 数据面实施计划

- Status: `draft`（等待 Topic 与录制边界对齐）
- Parent: `meta_plan.md`
- Branch: `main`
- Target: `w3-arx5`、ROS 2 Jazzy、单一 Privileged Docker Image

## 目标

在 `main` 建立只负责真实设备和 ROS 2 数据面的稳定核心：启动官方 SDK、发布固定逻辑 Topic、监督真实频率，并验证选定 Topic 能可靠写入 MCAP。

外层分支负责状态机、Hook、Store、metadata 和 CLI。两条开发线通过 ROS Topic、站点配置和已冻结的 Python Port 对接，不共享业务实现文件。

## 分支边界

`main` 实现：

- ARX5、RealSense SDK 构建、设备启动、容器与真机脚本。
- 三路 D405 ROS 2 Source，输出彩色图、对齐 Depth、相机信息和原始时间语义。
- 双臂 ROS 2 Adapter，将 Vendor 状态拆为六关节、夹爪、EEF 和示教输入。
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
- RealSense 使用稳定 `librealsense v2.54.2`；三路 1280×720 RGB-D @ 30 Hz。
- Depth 与同机彩色帧做空间对齐；禁止时间插值、补帧、重复帧和伪造同步帧。
- 所有 Topic 使用逻辑名称，不暴露 CAN、USB 和序列号。
- 默认不做 SHA 校验；运行报告保存在 Git 忽略的 `reports/`。
- `usbfs_memory_mb` 至少为 `256`；容器启动时检查并按需临时设置，失败立即退出。

## 建议 Topic 契约

每颗相机：

```text
/sensors/camera_<left|right|overview>/color/image_raw
/sensors/camera_<left|right|overview>/aligned_depth/image_raw
/sensors/camera_<left|right|overview>/camera_info
```

每只机械臂：

```text
/embodiments/<left|right>_arm/state
/embodiments/<left|right>_arm/eef_pose
/embodiments/<left|right>_arm/teaching_input
```

建议相机沿用标准 `sensor_msgs/Image` 和 `sensor_msgs/CameraInfo`。机械臂新增最小自定义 `ArmState` 消息，显式区分六关节、原始电流和夹爪，避免把夹爪伪装为第七关节；EEF 优先使用 `geometry_msgs/PoseStamped`。示教输入在真机辨识字段后冻结类型。

## 实施顺序

1. 盘点官方 ARX 状态消息、示教器字段和 EEF 语义，冻结消息定义与 Topic 叶子名称。
2. 建立 ROS 2 interfaces 与站点配置，加入消息契约测试。
3. 实现单相机单进程 Source；Launch 启动三进程，按序列号映射逻辑位置并执行 Depth 对齐。
4. 实现双臂 Adapter；只订阅官方状态并重新发布稳定逻辑消息。
5. 实现统一频率监督，报告帧数、平均频率、最大帧间隔和完全停流。
6. 在容器内验证 `rosbag2_py + rosbag2_storage_mcap` 对显式 Topic 的启动、停止、重复录制和干净关闭。
7. 外层 Port 合并后实现 `RecordingBackend` 与 `StreamMonitor` Adapter，不改变状态机或 Store。
8. 合并两条开发线，生成单一部署镜像并执行 90～150 秒真机 Episode 验收。

## 验收条件

- 容器重启后自动满足 USB 内存前提，三路 D405 和双臂均可见。
- 三路图像及对齐 Depth 连续 150 秒达到约 30 Hz，不伪造帧。
- 双臂稳定发布六关节、夹爪、原始电流和 EEF，实际频率可测；不产生采集侧运动命令。
- Topic 名称、消息类型与站点映射固定且有测试覆盖。
- 显式 Topic 可连续开始和停止录制，生成可读取 MCAP，无残留录制进程。
- 接入外层 Runtime 时只新增 Adapter 和配置，不修改状态机、Store 与 CLI 核心。

## 部署与回滚

- 继续使用 privileged container + host network；不做设备权限收敛。
- 真机开发副本保持独立目录与容器名，不修改 Vendor 既有目录。
- 新镜像失败时回退上一镜像；不得删除 Episode、`.partial` 或本地测试报告。

## 开放决策

1. 彩色消息使用设备原生 `yuyv` 还是 SDK 转换后的 `rgb8`；需结合 MCAP 吞吐和训练消费成本决定。
2. `ArmState` 的字段、单位和 EEF 坐标系；必须以官方消息和真机读数为准，不猜测。
3. `rosbag2_py` 能否直接满足单文件 `episode.mcap` 契约；不满足时先对齐，不擅自切换直接 MCAP Writer。
4. 外层分支冻结 `RecordingBackend`、`StreamMonitor` 后，ROS 2 Adapter 的最终包路径与合并顺序。

## 验收结果

待实施后回写。
