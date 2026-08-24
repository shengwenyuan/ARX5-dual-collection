# 相机数据面未来优化方向

- Status: `future; not scheduled`
- Scope: 三路 RGB-D 的 ROS 调度、DAgger Observation 与归档链路

短期维持当前实现；以下方向必须分别设计、压测和验收，不在 RGB8 切换中顺带实施。

## 零拷贝与 Shared Memory

优先评估 Fast DDS Shared Memory，减少本机大图像经过 UDP 与内核网络栈的开销；随后针对 Snapshot 设计共享内存槽位，只传递帧标识与时间戳。标准 `sensor_msgs/Image` 为动态数组，完整 loaned-message 零拷贝可能需要固定大小消息，会显著增加兼容成本，因此不作为近期默认方案。

## ROS 2 中间件调优

将图像数据面与 Snapshot、ArmState、控制事件等控制面隔离，重点评估独立 callback group、线程、异步有界发布、QoS、DDS flow controller 与 Recorder 缓存。优化不得通过放宽因果门槛、插值、补帧或隐藏丢帧实现；以帧率、消息丢失及 Snapshot P95/P99 延迟验收。

## H.265 归档

仅作为未来的 Color 归档支路：实时 Observation 保持原始 RGB8，硬件编码后的 H.265 单独落盘，并完整保存源时间戳、帧序号、PTS/DTS 与关键帧信息。普通 H.265 通常有损且采用色度降采样，引入随机访问与离线解码复杂度；未对齐允许的压缩损失和 MCAP 消息契约前不实施，Depth 始终保持独立的无损 Z16 链路。
