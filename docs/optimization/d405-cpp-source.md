# 三 D405 统一 C++ Source 计划

- Status: `RGB8 ordinary production validated at 848x480; DAgger RGB8 runtime statically validated`
- Updated: 2026-08-24
- Parent: `meta_plan.md`、`docs/sdk-ros2-core/implementation.md`、`docs/dagger/requirements.md`

## 目标

以一个 `rclcpp + librealsense C++` 进程统一拥有三颗 D405，维持既有六路 ROS Topic 与 MCAP 契约，并让 DAgger Snapshot 直接复用进程内真实 Color 帧，消除高带宽图像再次经过 ROS/DDS 订阅产生的积压。

## 架构

```text
multi_d405_source
├─ Pipeline left       ─┬─ Color / aligned Depth Publisher
├─ Pipeline overview   ─┼─ Color / aligned Depth Publisher
├─ Pipeline right      ─┴─ Color / aligned Depth Publisher
├─ Global Time 校验 + SDK queue capacity 1
├─ 进程内真实 Color 小历史 + 40 ms 邻近匹配
├─ 双臂轻量 ROS 历史 + 2 ms 因果匹配
└─ 可选 /dagger/get_snapshot
```

## 边界

- 一个进程、三条独立采集线程和三个独立 Pipeline；每颗相机只被一个 Pipeline 占用。
- 每相机继续执行 Depth-to-Color align；当前固定 848x480@30、RGB8、Z16、可靠 Topic 和 Global Time Header。
- 六路主线发布彼此独立，不等待跨相机配组；普通采集不启用 Snapshot service。
- D405 没有多机硬同步。`syncer` 或邻近匹配只从真实帧中选择，不改变曝光时刻；不插值、不补帧、不重复伪造帧。
- SDK 待处理队列容量为 1，避免消费端滞后；Snapshot Matcher 保留极小真实历史以适应独立 30 Hz 相位。
- Snapshot 仍使用 camera 40 ms、arm 2 ms、age 100 ms，不因失败放宽标准。
- Source 不包含 PI 协议、图像缩放、Policy、Recorder、metadata、控制权或动作下发。
- Python D405 Source 仅留作固定 RGB8 回归实现，不再由生产编排启动；在线链路不保留 YUYV 开关或回退分支。

## 已确认依据

- Python Snapshot Subscriber 在真机并发下出现秒级 callback backlog。
- 独立 C++ Snapshot Subscriber 的 MCAP 原速回放达到 95.2%，但 W3 真机仅 15/193（7.8%）成功，末尾积压约 1.04 秒。
- 同一真机 MCAP 离线复算：三相机 `<=40 ms` 为 1926/1927（99.95%），双臂年龄 99 分位小于 1 ms。问题位于二次 ROS 图像订阅，不在相机、CAN、Recorder 或门槛。

## 验收条件

- 三路 RGB-D 连续录制至少 45–60 秒，六路接近 30 Hz，Color/Depth 计数一致；稳定后恢复 90–150 秒验收。
- Topic、类型、编码、时间戳与 metadata 统计兼容，普通 Episode 无感知。
- DAgger Snapshot/Policy 成功率不低于 95%，不得连续超过 2 秒 Observation 失败，且每个成功组满足 40/2/100 ms。
- 单路失败使受控进程退出并由 Session 回收全部 Pipeline；退出后无相机残留占用。

## 初始实现结果（2026-08-19）

- 新增 `arx5_d405_source_cpp/multi_d405_source`，C++ 编译通过。
- 生产编排已从三个 Python Source 改为一个统一 Source；DAgger 不再启动独立高带宽 Snapshot Subscriber。
- 本地 Python 回归 224 passed、3 skipped；W3 `SnapshotBuffer` 4 项 gtest 通过。
- 候选镜像 `arx5-dual-collection:dagger-unified-d405` 已构建，并标记为 compose 使用的 `arx5-dual-collection:dagger`；最终 ID 为 `sha256:e38b36f35ab7f12b798292721b1e76f3267754da439d5af04e324456e31bf367`。尚未启动真机采集或相机。

## 848x480 复测决策

- 当前默认 RGB-D 规格由 1280x720@30 调整为 848x480@30；Color 与 aligned Depth 必须同时调整。
- 原始像素与数据负载降至 44.2%。60 秒三路 MCAP 实测参考由约 19.921 GB 降至约 8.800 GB。
- PI transport 仍输出 640x360 RGB；40/2/100 ms 因果门槛与 250 ms service deadline 不变。
- 本轮先复测现有 ROS service；若仍低于 95% 或连续失败超过 2 秒，再实施共享内存 transport。
- W3 候选镜像已部署为 `arx5-dual-collection:dagger`，ID `sha256:a25ef01d72b8f2001b4e45b2053f4c9a8d2ae2406edb425c0d521f0c31fca9f6`；尚未启动真机。

## 2026-08-19 848x480 DAgger 验收

- Episode `20260819T121334470523Z-74ac0291` success，连续录制 91.55 秒；六路图像均为 848x480，Color 为 YUYV、aligned Depth 为 16UC1，同相机 Color/Depth 计数一致。
- 三路相机约 29.98–29.99 Hz，双臂约 999.87–999.95 Hz；无 stream warning、无重复或倒退 Header 时间戳。
- MCAP 为 13.411 GB / 12.49 GiB，折算 60 秒为 8.79 GB / 8.19 GiB，与预期 44.2% 负载一致。
- Shadow 274/274 成功，成功率 100%，连续失败为 0；现有 ROS service 通过，不实施共享内存 transport。
- 原始 MCAP 三相机跨度中位数 14.36 ms、最大 31.19 ms，40 ms 下 100% 可配组；实际 Snapshot 请求全部满足 40/2/100 ms。

## 2026-08-19 普通模式回归

- W3 Episode `20260819T102326317556Z-ab397268` 正常 success，39.01 秒、11.9 GiB，退出后无相机、CAN、slcand、ROS 子进程或 Container 残留。
- 普通模式只启动一个 `multi_d405_source`，日志确认 `snapshot_service=disabled`。
- 双臂约 1000 Hz；三路 RGB-D 约 30 Hz。left/right 完全配对，overview 在 Recorder 边界多 1 帧 Color；right 出现一次 66.67 ms 孤立 gap。
- 离线复算三相机 40 ms 配组通过率 100%，最大跨度 28.53 ms；双臂因果年龄最大约 1.14 ms。
- 单 Episode 普通模式回归判定 PASS。提升为正式 production image 前，仍需在同一 Session 完成一条 45–60 秒和一条 15–30 秒连续 Episode，确认相机不在 Episode 间重启。

## 2026-08-24 RGB8 统一验收

- 提交 `c4f3841` 将 C++/Python D405 Source、Station 启流校验和 DAgger Observation 固定为 RGB8；在线 YUYV 实现全部删除，历史 YUYV 只由离线数据读取器兼容。
- W3 在同一普通 Session 完成两条 success 与一条 aborted。两条 success 分别录得三路各 1375 帧和 1060 帧 RGB-D；Color 全部为 `rgb8`、848x480、step 2544，Depth 保持 `16UC1`、848x480、step 1696。
- 三路相机均约 29.992 Hz，无重复或倒退 Header。两条 success 的同机 Color/Depth 全部逐帧同时间戳配对；aborted 仅在停止边界多一帧 overview Depth。
- 离线因果配组覆盖率为 100%，三条 Episode 最大跨相机跨度分别为 6.51、6.42、6.43 ms；没有跨相机或 arm age 拒绝。
- RGB 样帧确认通道顺序正确；未发现相机 fault、timeout 或 transport loss。一个 Source 连续覆盖三条 Episode，退出后容器、相机、CAN 和 ROS 子进程全部释放。
- 本轮不修改 Reliable QoS、队列、40/2/100 ms 因果标准或 Depth 数据面。普通采集通过；DAgger RGB8 Snapshot/resize 已通过静态与单元测试，真机 DAgger 验收另行执行。
