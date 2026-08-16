# 八路 ROS 2 数据面联合录制里程碑

- Status: `verified`
- Date: 2026-08-16
- Target: `w3-arx5`
- Artifact: `/reports/w3/2026-08-16/20260816T101141Z-eight-stream-validation`

## 验收范围

同时运行 ARX5 官方重力补偿、双臂逻辑 Adapter 和三颗 D405，显式录制六路 RGB-D、双路 ArmState 与轻量 telemetry。本里程碑只验收 ROS 2 数据面联合运行，不等同于完整 Episode 或 150 秒稳定性验收。

## 结果

| Stream | Count | Hz | Max gap |
|---|---:|---:|---:|
| left camera color / aligned depth | 1315 / 1315 | 29.953 | 66.670 ms |
| right camera color / aligned depth | 1317 / 1317 | 29.998 | 33.343 ms |
| overview camera color / aligned depth | 1316 / 1316 | 29.981 | 66.672 ms |
| left arm state | 43917 | 999.795 | 4.589 ms |
| right arm state | 43922 | 999.932 | 4.928 ms |

- MCAP 持续 43.925 秒，大小 13.6 GiB，共 96081 条消息。
- 八路 Topic 均为冻结的逻辑名称和消息类型；三个相机的同机 Color/Depth 计数分别完全一致。
- 八个 stream id 的 telemetry 全部存在；最终审计无低频 warning、无非单调 Header。
- Recorder 正常关闭，MCAP 与 `metadata.yaml` 可读取；停止后无 Source、Adapter 或 Recorder 残留进程。
- CAN1/CAN3 无 bus error、drop、restart 或 bus-off。
- 全程未插值、补帧、重复或伪造帧。

## 未关闭项

- 本次不是 150 秒正式稳定性验收；左相机与 overview 各出现一次约 66.67 ms 的真实帧间隔。
- ArmState Adapter 在关闭时出现无效 ROS context `RCLError`；数据已在此前完整落盘，但优雅关闭路径仍需修复。
- 官方 X5Controller 完成电机、线程和 SocketCAN 关闭流程后以 `-11` 退出；需单独界定 Vendor 退出行为。
- 当前产物是 rosbag2 目录，不是 Runtime 最终提交的 `episode.mcap + metadata.json`。
- 本轮逐台启动设备的 `/tmp` 编排脚本只用于验收，不进入主线。

## 结论

八路 ROS 2 数据面可在 w3 上同时运行并写入同一 MCAP，本阶段里程碑通过。下一阶段为修复项目 Adapter 关闭异常、接入 Episode Runtime，并完成 90～150 秒正式 Episode 与异常路径验收。
