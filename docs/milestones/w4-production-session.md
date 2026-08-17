# W4 Production Session 验收

- Date: `2026-08-17`
- Station: `w4-arx5`
- Status: `success-path-accepted`
- Plan: `docs/production-orchestration/implementation.md`

## 范围

W4 从标准 Docker Engine 和空 `/var/lib/arx5-collection` 开始部署 production image，通过唯一入口完成双臂、三颗 D405 和双踏板角色绑定。容器重启后 `arx5-collect devices` 七项全部 `matched=true`，随后多次生产 Session 启动与录制均成功。

本次静态审计选取 `fold_shirt-01` 下两条代表性 success Episode：

| Episode | metadata 时长 | MCAP 大小 | 双臂频率 | 三相机频率 |
| --- | ---: | ---: | ---: | ---: |
| `20260817T100408699229Z-addbb8cc` | 27.54 s | 9.10 GB | 999.78～999.89 Hz | 29.96～30.00 Hz |
| `20260817T100502823084Z-4a33947b` | 46.13 s | 15.27 GB | 999.80～999.93 Hz | 29.91～29.98 Hz |

## 结论

- 两条 MCAP 均可完整顺序扫描，固定八个 Topic、类型与 metadata 消息数完全一致；Header 时间戳无重复、逆序或伪造。
- 三颗相机各自的 RGB 与 Aligned Depth 全部逐帧同时间戳配对，无单机孤立帧。
- 相机存在孤立的约 66.7 ms 最大帧间隔，没有持续掉流；双臂最大间隔小于 5 ms。
- 八路共同有效交集分别为 27.34 秒和 45.96 秒，覆盖几乎完整录制窗口。
- 三机独立采样事实保持不变：left 到 right 最近帧中位偏差约 10 ms，left 到 overview 约 14.5 ms，不声明硬件级跨相机同步。
- 两条 metadata 均为 `success`、`errors=[]`、Station=`w4`，设备身份完整；内外参按 v0.1 边界保持 `null`。
- 原始写入速率约 331 MB/s，与 YUYV + Z16 负载预期一致；90～150 秒 Episode 预计占用约 30～50 GB。
- `/monitoring/stream_status` 只用于在线监督，不作为第九路录入 MCAP；最终八路指标由落盘审计写入 metadata。

W4 Station 初始化与生产 success 路径验收通过，可以开始批量数据采集。90～150 秒完整八路 Episode、必需流故障注入与长期压力仍由生产编排计划跟踪，不在本里程碑提前宣称完成。
