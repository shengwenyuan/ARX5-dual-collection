# w5 collection 真机录制验收：2026-09-20

结论：**本批正常多局操作与大部分输出契约得到验证，但 collection 完整验收暂不通过，不锁定完成版本或上层 submodule。** 19 局中有 6 局缺失首条动作记录；长按/释放、录制中 Ctrl+C、双踏板冲突尚未实测。模型任务成功率不作为 collection 验收门槛。

## 范围与证据

- 用户在 w5／ARX-ACO-003 录制；本轮只读检查原始数据，未移动、删除、补写或重新压缩任何 episode，未停止用户仍在运行的 policy-server。
- 数据根目录：`/home/lenovo/swy/reports/2026-09-20/slot4-infer-acceptance`。
- 同一 session `20260920T122828Z`，当地时间约 20:28:44–20:36:18；19 局，18,027,994,965 字节 MCAP。
- 人工结果：3 success、16 fail，success 比例 15.8%。均为 `human_label`；右踏板 success、左踏板 fail，`errors=[]`、`recording_completed=true`，无遗留 `.partial`。
- 实际 collector 镜像为 `ed5e7447389d`，退出码 0、非 OOM；统一配置/加载接入已部署，checkpoint 为 `w5_0918_0919_slot4_old502_v1_20260920-0130/29999`。
- 复用仓库已有 rosbag2 reader、HeaderTiming 和 Header 时间解析，逐条读取全部 MCAP；使用既有 `mcap doctor`，并用独立的 `mcap filter` 抽取动作话题交叉核实 6 个异常样本。检查脚本只作为本轮验收证据，不新增产品运行审计系统。
- 19 份 metadata 均通过仓库 JSON schema。checkpoint 和 policy/station/task 配置身份一致；3 份运行配置 SHA 与设备文件相符。原始文件在回读前后的尺寸与 mtime 一致。

证据目录：w5 `/var/lib/arx5-collection/deployments/20260920-collection-acceptance/`。包含回读结果、原 metadata 副本、运行日志、检查脚本、各 MCAP doctor 输出及 6 份仅动作话题的交叉检查副本。逐局处置清单见 [acceptance-w5-20260920.json](acceptance-w5-20260920.json)。

## 已通过的检查

| 检查 | 结果 |
| --- | --- |
| 人工标签与落盘目录 | 19/19 对应正确；普通 fail 正常保存，不误记为运行故障 |
| 多局 HOME / 录制 / 等待 | 日志有 19 次 HOME、19 次 RUNNING、19 次提交、20 次 READY；用户确认落盘后等待新右踏板才 HOME／下一局 |
| HOME 与 episode 边界 | 每局 HOME 完成后才启动 Recorder；已录动作的时间均处于本局起止范围，多局时间不重叠 |
| MCAP 文件结构与传感器 | 19/19 可完整回读，doctor 返回 0；双臂状态和三路 RGB 均存在，计数与 metadata 一致 |
| 动作值与时间字段 | 已录 7,788 条命令均为对应 episode 的有限 14D 数据；ROS/单调时间递增，夹爪在既有 raw 限幅内 |
| 实际记录频率 | 每局动作 Header/单调时间跨度计算的平均频率为 49.976–50.038 Hz |
| 停止与尾部采样 | 本批无人工标注之后的新动作记录；标注到动作停止标记为 10.184–11.454 ms，停止后保留 100.068–101.766 ms 录制机会；各传感器末帧晚于最后动作 59.869–129.006 ms |

这不证明硬件 ACK 或每个目标已经到位：记录契约是已下发目标。传感器检查只报告本批观察结果，完整训练对齐/质量策略仍由 exporter 负责。

`mcap doctor` 共报告 3,858 条跨话题 log_time 顺序告警，无其他 doctor 输出；这是既有 finalizer 已单独处理的告警类型，没有据此判为结构损坏。各传感器自身 Header 时间均递增，图像约 29.993 Hz、双臂约 1,000 Hz。后续对齐应使用 Header/命令发送时间，不能把 MCAP 接收 log_time 当实际下发时刻。

## 阻塞项：6 局缺失 step 0

metadata 共计数 7,794 次发送，而 MCAP 实际记录 7,788 条，相差 6 条。异常局均恰好少 `step_index=0`，从 1 开始，之后连续到末条；末条时间与 metadata 一致。

| Episode | 结果 | metadata 动作数 | MCAP 动作数 | 缺失 |
| --- | --- | ---: | ---: | --- |
| `20260920T123417544683Z-b670a9ef` | success | 418 | 417 | step 0 |
| `20260920T123441685634Z-1478a221` | fail | 380 | 379 | step 0 |
| `20260920T123502979569Z-6c341f3c` | fail | 381 | 380 | step 0 |
| `20260920T123520557087Z-363e5037` | fail | 645 | 644 | step 0 |
| `20260920T123549613496Z-ccbda5b9` | fail | 416 | 415 | step 0 |
| `20260920T123610901408Z-9ccc3b87` | fail | 360 | 359 | step 0 |

其中 1 局 success、5 局 fail；另 13 局（2 success、11 fail）通过本次 collection 输出契约检查。按既定首版策略，6 局应整局隔离，保留原始数据及原因，不裁首步、重编号或插值抢救。本轮仅记录处置清单，未移动原始目录，也未将另外 13 局直接宣布为 exporter/replay 已接纳。

6 局最早保留下来的 step 1，其 MCAP log_time 比命令 Header 时间晚约 5.10 秒。这是录制传输时序的排查线索。优先核查每局新 Recorder 与持续存在的命令 publisher 在启动时的 DDS 匹配/可靠传输；**根因尚未确认**。不能仅凭 metadata 的 recording_completed 或发送计数声称每条动作已进入 MCAP，也不能把该问题归因于模型成功率或据此修改 50 Hz 调度逻辑。

## 剩余验收

1. 修复并复测首条命令缺失，要求连续多局每局从 step 0 起、MCAP 计数与发送计数一致；复用现有 Recorder 与采集模式，不扩张为完整追踪系统。
2. 用户明确本批只按正常流程操作：长按/释放、录制中 Ctrl+C、双踏板冲突均未验证，保持待验收。现有离线测试不能代替这些现场行为。
3. exporter 的 fail 接纳、关键缺失整局隔离、动作对齐和 replay 联调仍单独执行；本批可作为后续成功、正常失败、缺首动作的真实样例。

本次只提交验收事实与清单，没有修改运行代码、模型、调度器或现场镜像。
