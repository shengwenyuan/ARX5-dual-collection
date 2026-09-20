# infer / DAgger 录制 publisher 按局管理修复

2026-09-20。**修复已通过无真机验证，22:03 已更新 w5 采集镜像；用户随后录制了 10 局，动作话题回读及剩余现场验收待完成。** 共用模型、RTC scheduler、控制频率、消息契约和 DDS 配置均未修改。

## 检查与边界

- `/infer/command` 和 `/dagger/authority` 均存在“publisher 跨局、Recorder 按局”的生命周期。两者共用新的 `RosRecordingPublisher`，只按局创建/销毁底层 publisher，保留 node/context。
- DAgger takeover 与 takeover dry-run 都接入；shadow 只写已有 JSONL，没有同类 ROS 录制 publisher。
- `RosDualArmControlPort` 的机械臂命令 publisher 服务持续存在的控制端，不是此次按局录制事实的 publisher；原任务流也未把这些命令 topic 列作录制流。本轮不改它的生命周期。
- DAgger 业务字段 `sequence` 仍按既有 AuthorityTimeline 递增，未按局重置；只重置 DDS writer 自身序号。

## 实现

共享能力位于 [recording_publisher.py](../../src/arx5_collection/ros2_adapters/recording_publisher.py)，由现有 [RosbagRecordingBackend](../../src/arx5_collection/ros2_adapters/recording.py) 管理：

1. 原有 HOME/前置检查完成后，backend 创建本局 writer，再启动 Recorder。
2. 原有图就绪检查通过后，等待本局 Recorder 身份和 RMW 匹配；两者满足后 backend.start 才返回，后续 controller hook 才能发第一条。
3. 原停止 hook 关闭动作/权威转移并保留既有录制尾部；Recorder 结束后销毁 writer，再完成后续 metadata/提交。
4. 创建、图就绪、匹配、录制和结束失败均回收本局 writer；context/node 随应用退出关闭。未增加 executor、模型分支、审计系统或自动修复数据。

infer/authority 原消息编码逻辑保留，RELIABLE 与 depth 256/32 不变。应用层仅将各自 publisher 交给同一个 backend 能力。匹配仍不等于数据 ACK；消除本次累计序号缺陷依赖按局创建新 writer。

## 无真机结果

使用 w5 既有镜像 `ed5e7447389de7b383aab0c076507a292062e68dbe01803c5959dc62ec90db8c`，通过只读源代码挂载测试修复，不构建镜像、不下载依赖。独立 network/IPC，1 GiB SHM，domain 177–179，无设备/GPU映射；不连接模型、相机或机械臂。用户已有 policy-server 未操作。

| 组别 | 完整局数 | 回读 / 发送 | 最大接收延迟 |
| --- | ---: | ---: | ---: |
| 旧版 `/dagger/authority` | 13/20 | 7,993 / 8,000 | 620.522 ms |
| 修复后 `/infer/command` | **20/20** | **8,000 / 8,000** | **0.561 ms** |
| 修复后 `/dagger/authority` | **20/20** | **8,000 / 8,000** | **0.495 ms** |

每组 20 局、每局 400 条、50 Hz；逐局读回 MCAP，核对完整步序、无重复、时间字段、动作或 authority 业务字段。旧 authority 在第 14–20 局均缺首条，约 0.62 秒积压，与 depth=32 一致。这是加速累计序号的合成事件压力测试，不声称正常人类踏板频率下第 14 局就会出错。

修复后调用真实的 `RosbagRecordingBackend.start/stop` 与 publisher 实现，没有在测试中手动替代重建逻辑；每局检查同一 node/context 保留，停止后 publisher 已释放。未新增延迟来掩盖问题；沿用对照的 0.2 秒 bootstrap、0.1 秒尾部和 1 秒局间等待。infer 旧版复现参见 [前轮定位](command-transport-root-cause-20260920.md)。

全量离线测试 **431 passed、1 skipped**，涵盖新 writer/保留 node/context、QoS 不变、错误 Recorder 或仅图就绪不能放行、初始化失败、Recorder 创建/写入/匹配/停止/落盘异常、monitor/start/stop hook 异常、录制中断后的清理，以及 infer、DAgger takeover/dry-run 应用接线。保留既有模型/调度回归。所有测试停在真机验证前。

## 复现、归档与清理

保留的当前回归入口：[recording_publishers.py](../../scripts/diagnostics/recording_publishers.py)。`--topic infer` 或 `--topic authority`，默认 20 局、400 条；`--legacy` 仅用于旧镜像基线。

在现有 ROS 镜像环境中，将源码目录、脚本目录及空输出目录分别只读/只读/可写挂载为 `/work`、`/test`、`/results`，通过已有 `/ros_entrypoint.sh` 启动：

```bash
PYTHONPATH=/work/src:$PYTHONPATH python3 /test/recording_publishers.py \
  --topic infer --output /results/infer-after
```

保留原镜像 PYTHONPATH 的 ROS 消息包路径，仅前置修复源码；不要覆盖为只有 `/work/src`。authority 使用另一个空输出目录和 `--topic authority`。测试时维持 `--network none`、独立 IPC、无设备映射。

[逐局结果与源码校验](publisher-lifecycle-fix-20260920.json)对应本轮实际运行源码。w5 完整证据：`/var/lib/arx5-collection/deployments/20260920-publisher-lifecycle-fix/`，包含 60 份 MCAP、收发时间、日志、隔离配置及测试源码快照。前轮一次性 DDS 探针/旧入口仅保留在其历史归档，已从当前 scripts/diagnostics 移除；本轮临时工作目录、传输包和脚本副本在归档校验后清理。

独立的 `FASTRTPS_DEFAULT_PROFILES_FILE` 兼容问题仍未改动；本轮通过保持原配置验证 publisher 修复。已知 6 局异常数据已按用户要求删除。现场镜像已更新，用户已追加录制 10 局；动作话题回读、足够连续局数与剩余踏板边界验收仍待完成，此前不锁定上层完成版本。

## w5 部署与数据整理

22:03 在原镜像上离线 COPY 六个已验证的 Python 文件，未下载依赖。镜像 `0618057fa6195538a9852fcb6df9c69e32148a93c7ddbffa0e8f4b865e221d8f` 的 dagger/infer/production/calibration 标签和宿主机源码已同步；源码 SHA 与 20 局测试快照一致。实际新镜像的两个话题各 2 局、800 条完整，无积压；最大延迟分别为 0.471 ms、0.362 ms。

旧采集容器和旧镜像已清理，现有 policy-server 未重启且保持 healthy，配置与 reports 路径不变。部署证据位于 w5 `/var/lib/arx5-collection/deployments/20260920-publisher-deployed/`，SHA256SUMS 校验通过；临时构建和传输文件已清理。

用户随后录制 10 局：4 success、6 fail。按明确指令删除上一批 6 局异常（1 success、5 fail），并将剩余两批 23 局平铺到 `/home/lenovo/swy/reports/2026-09-20/slot4-infer-acceptance/`；移动前后 metadata SHA、MCAP inode 和大小均一致。保留数据共 6 success、17 fail，成功率 26.1%，episode 时长累计 197.118 秒；成功片段累计 51.963 秒。该统计来自 metadata，不代表新批次 MCAP 动作链路已验收。

删除与合并的逐局记录见 [数据整理记录](batch-cleanup-20260920.json)。上轮 acceptance JSON 是删除前的历史验收快照，不是当前目录清单。仍需完成新批次动作回读及剩余真机边界测试。
