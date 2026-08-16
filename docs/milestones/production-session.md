# Production Session 短链路里程碑

- Date: `2026-08-16`
- Station: `w3-arx5`
- Status: `accepted-with-boundary-note`
- Plan: `docs/production-orchestration/implementation.md`

## 验收范围

- `arx5-collect` 作为容器主进程，全权启动一次 usbfs、双路 USB2CAN/CAN、ARX5 v2_collect、Arm Adapter 和三颗 D405。
- 五设备序列号和三颗相机 USB 3.2 状态通过统一 `devices`/Session 检查。
- 八路真实 telemetry 与固定 Topic 类型全部 READY 后，连续录制两条 Episode；两条之间未重启 CAN、ARX5 或相机。
- 两条 Episode 均由 `SPACE` 开始、`SPACE` success 结束，随后在 READY 退出 Session。

## 产物结论

| Episode | 时长 | MCAP | 双臂 | 三路 RGB-D |
| --- | ---: | ---: | --- | --- |
| `20260816T133517402955Z-d813f0d8` | 21.421 s | 6.96 GB | 20,949 / 20,948，约 1000 Hz | 约 30 Hz，无 warning |
| `20260816T133547173010Z-b0c9cc47` | 31.858 s | 10.35 GB | 31,285 / 31,283，约 999.5 Hz | 约 30 Hz，无 warning |

- 每条正式目录都严格只有 `episode.mcap + metadata.json`，outcome 为 `success`，设备身份完整。
- MCAP 直接扫描确认八个 Topic 和类型正确，全部 Header 时间戳单调，无插值、补帧、伪造帧或 SHA。
- `/monitoring/stream_status` 仅用于 Session/运行期监督，不作为第九路写入正式 MCAP；最终 metadata 指标直接审计八路 MCAP。

## RGB-D 配对

- Episode 1：右 628 对、overview 627 对；左 628 对并在录制开启边界保留 1 个孤立 Depth 真实消息。
- Episode 2：左 942 对、右 939 对、overview 921 对，全部按相同 Header 时间戳严格配对。
- 已知边界行为不做裁剪、插值或伪造；后续是否要求 Episode 级零孤帧，需要独立对齐 Source/Recorder 边界契约。

## 退出与修复

- 首次人工退出连续输入两次 `Ctrl+C`，第二次中断 shutdown wait，资源仍完整回收，但程序返回 cleanup failure。
- `d77408c` 已在受控清理阶段屏蔽重复 INT/TERM；w3 连续注入两次 INT 后 `EXIT=0`，无 cleanup error。
- 最终无容器、slcand、X5Controller、Adapter、D405 Source、can1 或 can3 残留；Vendor Controller 请求关闭后的 `-11` 继续按已知 Vendor warning 处理。

## 剩余验收

- 90～150 秒正式 Episode。
- 录制中停止一个必需 Source，验证 `aborted + 可读 MCAP + metadata + 全量回收`。

