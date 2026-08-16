# D405 C++ Source 优化计划

- Status: `aligned`（deferred）
- Parent: `meta_plan.md`、`docs/sdk-ros2-core/implementation.md`
- Start after: Python 三相机 Source 与完整 v0.1 Episode 通过真机验收

## 目标

将三路 D405 高带宽数据链路从 `rclpy + pyrealsense2` 迁移为 `rclcpp + librealsense C++`，减少消息构造抖动和额外内存复制，同时保持上层 Runtime 无感知。

## 范围与边界

- 每颗 D405 使用独立进程，负责取帧、单机 Depth 对齐、图像发布和必要的编码转换。
- 继续使用当前稳定 librealsense、序列号映射、Global Time 和站点配置。
- Topic 名称、`sensor_msgs/Image` 类型、时间语义及 MCAP 契约保持不变。
- 不修改 Episode 状态机、Hook、Store、metadata、CLI、双臂 Adapter 或跨相机同步策略。
- 不增加插值、补帧、配对等待、伪同步、IR 或 `CameraInfo`。

## 进入条件

- Python Source 已完成三路 1280×720@30、150 秒及端到端 Episode 验收。
- 已记录 Python 基线：实际频率、最大帧间隔、CPU、内存、USB 与 MCAP 写入吞吐。
- 上层 Topic、消息和站点配置契约已经冻结。

## 实施步骤

1. 建立 Python Source 的回归数据与性能基线。
2. 实现可配置的单相机 `rclcpp + librealsense C++` Source，并保持相同 Launch 参数和错误语义。
3. 启动三个独立进程，复测 YUYV、RGB8 与 Depth 对齐路径。
4. 对 Python/C++ 执行同机同配置 A/B，比较频率、帧间隔、CPU、内存和 MCAP 吞吐。
5. 完成长时间与单路掉线测试；通过后只替换 Launch 实现，不改变上层配置。

## 验收条件

- 三路 1280×720 RGB-D @ 30 Hz 连续运行和录制至少 150 秒。
- Topic、消息类型、时间戳来源和 metadata 统计与 Python 实现兼容。
- 单路故障不阻塞其他相机，退出时无残留进程和持续资源增长。
- 相比 Python 基线具有可复现的资源占用或帧间隔改善；若无实际收益，保留 Python 实现作为默认路径。

## 验收结果

尚未实施。
