# 真机接入与最小采集链路

- Status: `in-progress`（SDK 与三路相机基线已通过，继续机械臂频率与最小录制链路）
- Target: `w3-arx5`
- Runtime: Ubuntu 24.04、ROS 2 Jazzy、Privileged Docker、Host Network

## 目标

确认双臂、三颗 D405 和采集主机的真实连接条件，建立可重复部署的只读设备探测与最小录制链路，为完整 Episode 采集开发提供确定基线。

## 范围

本轮实现：

- 盘点主机、Docker、ROS 2、CAN、USB、SDK、磁盘和已有源码环境。
- 确认左右机械臂 CAN 映射，使用官方 `remote_master` 重力补偿并读取状态。
- 确认三颗 D405 的序列号、USB 拓扑和 1280×720 RGB-D @ 30 Hz 能力。
- 调查 RealSense 官方 SDK 的多设备时间戳与软件同步能力。
- 建立 Privileged Docker + Host Network 的部署入口。
- 建立键盘触发接口，并为脚踏板适配器保留边界。
- 建立设备探测、频率统计和 MCAP + JSON 最小录制骨架。

本轮不做：

- 机械臂回零、位置、关节、夹爪、EEF 或其他运动命令。官方 `G_COMPENSATION` 初始化是唯一例外。
- DAgger、UI、人工 `fail` 标注和远程任务分发。
- 插值、补帧、软件放大或伪造同步帧。
- 设备权限最小化和通用 SHA 校验。

## 模块边界

- `device-probe`：输出主机、CAN、USB、RealSense 与 ROS 2 可用性；动态结果使用 JSON 写入本地 `reports/`，不纳入 Git。
- `camera-bringup`：按序列号绑定逻辑相机，发布固定 `/sensors/...` Topic。
- `embodiment-bringup`：按站点配置绑定左右机械臂，发布固定 `/embodiments/...` 状态 Topic。
- `record-trigger`：统一开始与结束事件；v0.1 使用键盘适配器。
- `episode-recorder`：写入临时目录，关闭成功后提交 MCAP 与 JSON。

具体语言和 Vendor SDK 接入方式以真机盘点结果为准，避免在发现现有能力前重复实现驱动。

## SDK 基线

- ARX5：仅使用官方 [`ARXroboticsX/ARX_X5`](https://github.com/ARXroboticsX/ARX_X5) `main`，构建仓库内 ROS 2 workspace；禁止引入 `ARX5_beta`。
- ARX5 启动：采用官方 `v2_collect`、`arm_end_type: 2`、左 `can1`、右 `can3`、`remote_master`；不发布 `/arx_joy` 和运动控制 Topic。
- D405：使用官方稳定 `librealsense v2.54.2`，与当前固件 `5.15.1.55` 对齐；不刷固件。
- Ubuntu 24.04 兼容：构建时为 v2.54.2 的 `rsutils/version.h` 补充缺失的 `<cstdint>`；补丁不改变 API、算法或设备逻辑，升级 SDK 时必须重新评估并优先移除。
- RealSense ROS2：本轮不引入 beta Wrapper。相机 ROS 2 适配在后续模块中基于稳定 SDK 实现。
- 版本只在文档和 Docker 构建参数中标记，不增加 SHA 校验。

## 实施步骤

1. 只读盘点 `w3-arx5`，记录系统版本、设备节点、网络接口、USB 树、磁盘和现有安装。
2. 识别三颗 D405，并验证单机及三机并发流配置。
3. 构建官方 ARX5 ROS 2 SDK，启动双臂 `remote_master` 重力补偿并验证状态读取；禁止其他控制入口。
4. 依据盘点结果实现设备探测 CLI、容器入口和最小 ROS 2 Launch。
5. 在容器内验证逻辑 Topic、键盘触发、临时 Episode 与异常关闭。
6. 执行 150 秒三相机基准和机械臂状态频率统计。
7. 回写真实设备映射、测试结果、偏差和下一阶段约束。

## 验收条件

- 容器能看到真实 CAN 接口和三颗 D405。
- 设备探测 CLI 输出稳定 JSON；除官方重力补偿初始化外，不发送机械臂控制指令。
- 三颗 D405 可按序列号稳定映射为 left、right、overview。
- 三路 1280×720 RGB-D @ 30 Hz 连续运行至少 150 秒，并报告真实频率与最大帧间隔。
- 左右机械臂状态 Topic 可读，并报告实际频率。
- 键盘可连续开始和结束 Episode。
- Episode 正常生成 `episode.mcap + metadata.json`；必需 Topic 停止时结果为 `aborted`。
- MCAP 可读取，Topic 名称符合 `meta_plan.md`。

## 部署与回滚

- 当前开发副本部署在 `w3-arx5:/home/lenovo/swy/ARX5-dual-collection-dev`，不修改已有 ARX5 目录。
- 使用 `docker/compose.bringup.yaml` 构建和启动单个 bring-up 容器；容器内管理 `slcand`，停止容器时自动回收 CAN 接口。
- 使用独立容器名和项目目录，不覆盖主机已有 Vendor SDK 与用户文件。
- 只停止和替换本项目创建的容器；删除或修改既有系统服务前必须重新对齐。

## 验收结果

2026-08-15 已完成首轮真机验收：

- 主机为 Ubuntu 24.04、ROS 2 Jazzy、Docker 29.7.2；NVMe 可用空间约 1.8 TB。
- 左臂 `0045002B5330530320323656 → can1`，右臂 `004E002E5330530320323656 → can3`；容器停止后两路 CAN 均已回收。
- 旧环境完成过双臂构造、状态 Getter 和 `close` 探测：两秒 121 次读取，约 62.5 Hz，最大间隔 16.81 ms，无 fault、丢包或总线错误。该结果不作为本轮官方 `ARX_X5` 基线验收，必须重测。
- SDK 的组合反馈为 `[J1..J6, gripper]`；采集契约必须拆成六关节与独立夹爪字段。
- 三颗 D405 均被 SDK 2.58.3 识别，`global_time_enabled=1`，不支持 `inter_cam_sync_mode`。
- `261122270651` 为 USB3.2，可在 640×480 RGB-D @ 30 Hz 下稳定输出；`261122270960`、`261022274824` 当前为 USB2.1，无法建立 1280×720 流，降至 640×480 后仍无帧。
- 1280×720 三路 150 秒验收、相机逻辑位置映射、键盘 Episode 和 MCAP 提交尚未通过，不得标记整体验收完成。

原始结果保存在本地 `reports/w3/2026-08-15/`，该目录用于动态更新当前真机状态，不纳入 Git；可复现的关键结论必须回写本计划。下一步必须先把两颗 USB2 D405 改接至 USB3 端口、线缆或 Hub，再重复三路基准；不降低正式分辨率标准。

2026-08-15 已完成官方 SDK 基线部署与第二轮短测：

- `arx5-dual-bringup:dev` 已从官方源码构建：ROS 2 Jazzy、`ARXroboticsX/ARX_X5:main`、`librealsense v2.54.2`、Python binding 和 MCAP storage plugin。
- v2.54.2 在 Ubuntu 24.04 / GCC 13 下使用已批准的 `<cstdint>` 单行兼容补丁；镜像内 SDK 自检通过。
- 官方 `v2_collect` 已在 `can1/can3` 启动双臂 `remote_master`，左右状态 Topic 均可读取；测试结束时两路 CAN 均为 `ERROR-ACTIVE`，总线错误、丢包和 bus-off 均为 0，容器停止后 CAN 已回收。
- 官方节点会高频输出“ARX方舟无限”；后续封装需处理日志噪声，但不得修改其重力补偿行为。
- 三颗 D405 现均枚举为 USB 3.2。三台分别可完成 1280×720 RGB-D @ 30 Hz 短测；三机同时启动时只能稳定两路，最后启动的一路超时。
- 三颗相机共享同一个 Intel xHCI 控制器；该轮结束时主机 `usbfs_memory_mb=16`，因此三机并发 150 秒验收尚未通过。

该轮计划保持 `in-progress`，等待授权调整 `usbfs_memory_mb` 后复测。

2026-08-15 已完成三相机并发正式验收：

- 经授权临时将主机 `usbfs_memory_mb` 从 `16` 调整为 `256`；未写入持久配置，主机重启后恢复默认值。
- 三路 1280×720 RGB-D @ 30 Hz 的 10 秒短测全部通过，实际频率为 30.03–30.05 Hz。
- 三路 150 秒正式验收全部通过：每路捕获 4486 帧，实际频率为 30.002–30.004 Hz，最大帧间隔为 40.4–44.6 ms，无超时或缺失对齐帧。
- `usbfs_memory_mb=256` 是当前三路 D405 并发的部署前提。后续应在容器启动前检查并设置该参数，是否持久化到主机配置需另行对齐。

相机 SDK 与三路并发基线已通过；整体计划仍为 `in-progress`，剩余机械臂官方 Topic 频率、键盘 Episode、MCAP + JSON 提交和异常路径验收。
