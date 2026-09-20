# `/infer/command` 无真机传输实验：2026-09-20

后续更新：[第二轮定位](command-transport-root-cause-20260920.md)已通过累计序号对照和底层拒绝返回定位原因，并验证仅重建 publisher 实体即可规避；下文保留首轮实验的结论边界。

**A/B 均从第 14 局开始稳定复现；C 20/20 局完整。单独增加匹配等待无效，每局重建发布端生命周期在本次受控实验中消除了现象。尚未修改生产代码，也未确认 DDS 内部导致首条无法交付的具体机制。**

| 组别 | 唯一条件差异 | 完整 episode | 回读消息 / 发送消息 | 指定异常特征 | 最大接收延迟 |
| --- | --- | ---: | ---: | --- | ---: |
| A | 跨局复用 publisher；原有 Recorder 图就绪检查 | 13/20 | 7,993 / 8,000 | 第 14–20 局，均缺 step 0 | 5100.266 ms |
| B | A + 首条前显式等待本局匹配 | 13/20 | 7,993 / 8,000 | 第 14–20 局，均缺 step 0 | 5100.134 ms |
| C | B + 每局重建 RosCommandPublisher | 20/20 | 8,000 / 8,000 | 未出现；无缺失/重复 | 0.280 ms |

## 固定条件

- 在 w5 使用已部署 collector 镜像 `sha256:ed5e7447389de7b383aab0c076507a292062e68dbe01803c5959dc62ec90db8c`，不构建镜像、不下载依赖。
- 每组独立容器，按 A → B → C 顺序执行；`--network none`、独立 IPC、1 GiB SHM、`ROS_DOMAIN_ID=177`，无设备/GPU映射、非 privileged。不启动模型、相机或机械臂，不操作用户仍在运行的 policy-server。
- 使用镜像中实际安装的 `RosCommandPublisher`、`InferCommand`、`RosbagRecordingBackend`、Recorder factory 和原有图就绪检查，不复制实现或替换 Recorder。
- 只录制 `/infer/command`，每局 400 条，50 Hz，RELIABLE、depth 256。合成 14D 动作可逐条验证；不调用机械臂控制接口。
- 每组至少 20 局。A 若前 20 局没有复现指定特征，则在**同一进程、同一 publisher** 下延长至 50 局；本次已复现，因此 A 止于 20 局。
- 三组相同：Recorder 就绪后固定等待 0.2 秒模拟 bootstrap；末条发送后保留 0.1 秒再停止；局间等待 1 秒。无探测消息、ACK 等待、额外订阅端或 QoS 修改。
- 三组均由同样的只读观察线程每 2 ms 查询匹配状态，记录 RMW `get_subscription_count()==1`，且唯一订阅端名称确为本局 Recorder。A 只观察；B/C 首次发送前等待该条件。该 API 状态**不代表首条数据已经接收或 ACK**。
- C 每局重新进入/退出现有 `RosCommandPublisher`，包括其 rclpy context、node 和 publisher。实验没有进一步区分这三个对象中哪一层持久状态导致异常。

FastDDS `2.14.6`、`rmw_fastrtps_cpp 8.4.4`、rosbag2 `0.26.11`；完整包版本及 FastDDS 配置路径保存在证据目录。

## 回读与时序判定

每局停止后回读 MCAP，逐条检查 episode_id、合成动作值、发送时间与发布端输入一致；检查步号完整为 `0..399`、无重复/跨局数据。接收时间取 MCAP log timestamp，发送时间取消息 Header；两者使用同机系统时间。就绪、匹配、发送之间的先后用单调时钟比较。

每局保存 `events.json`、`sent.json`、`received.json`、`result.json` 和原始 `episode.mcap`。异常特征判定单独要求：只缺 step 0；step 1 延迟 4.5–6 秒；steps 1–256 在 ≤10 ms 内集中接收；step 257 起接收延迟中位数 <100 ms。该阈值用于复现归类，实际测量值见下文及汇总 JSON。

## 结果解释与修复边界

- A/B 共 14 个异常 episode，全部恰好缺 step 0，steps 1–399 完整、无重复；step 1 接收延迟为 5099.984–5100.266 ms；steps 1–256 在 0.067–0.227 ms 内集中到达；step 257 起延迟中位数恢复至 0.078–0.227 ms。
- 这些异常局在首条发送前 287.589–355.701 ms 已观察到本局匹配，Recorder 图就绪也均早于首条约 200 ms。B 显式等待仍复现，因此“只补等待订阅匹配”不是本次故障的有效修复。
- 三组发送端均生成 400 条，发送跨度约 7.98 秒；C 的 8,000 条全部原样进入 MCAP。异常不依赖模型、相机或机械臂，也不是本合成测试中的 50 Hz 发送端漏发 step 0。
- 现象与 depth 256 的可靠交付积压解释一致，但本轮未抓取 DDS ACK/NACK/序列日志，不能据此断言 step 0 最初丢在何处，或指定 FastDDS/SHM 内部某个缺陷。
- 下一步最小候选修复：使 infer 命令发布端按 episode 创建/释放，沿用现有 Recorder 和匹配检查；不调整模型、RTC 调度、50 Hz 或缓存深度。C 同时重建了 context/node/publisher，不能把效果缩窄为仅重建底层 writer 已证实有效。
- C 只证明当前无传感器、同机隔离环境下连续 20 局通过；生产接线修改后仍须用实际路径连续回归，再做真机录制验收。没有单独测试“每局重建但不等待匹配”，不推断等待是否可以删除。

## 可复现入口与证据

实际运行脚本：w5 历史归档中的 `20260920-command-transport/diagnose_infer_command_transport.py`。SHA256：`c13adeb2e319af9b7afe455149b8c15a10e58f8a249f7b1c0ccdbfd3bdb502f0`。这是离线诊断入口，不接入正常采集流程，不新增 collection 运行审计系统。

下面在 w5 执行，每组使用新的空输出路径。先执行 A、检查结果，再依次将 `GROUP` 改为 B、C；如果 A 到 50 局仍不复现，应停止比较并保留未复现结论。

```bash
GROUP=A
SCRIPT=/tmp/diagnose_infer_command_transport.py
RESULTS=/tmp/arx5-command-transport-new-run
mkdir -p "$RESULTS"
docker run --rm --name "arx5-command-transport-$GROUP" \
  --network none --shm-size 1g \
  -e ROS_DOMAIN_ID=177 -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint /ros_entrypoint.sh \
  --mount "type=bind,src=$SCRIPT,dst=/test.py,readonly" \
  --mount "type=bind,src=$RESULTS,dst=/results" \
  sha256:ed5e7447389de7b383aab0c076507a292062e68dbe01803c5959dc62ec90db8c \
  python3 /test.py --group "$GROUP" --episodes 20 --extend-to 50 \
  --output "/results/$GROUP" > "$RESULTS/$GROUP.log" 2>&1
```

运行前把历史归档中的冻结脚本复制到 `$SCRIPT`；该旧入口已从当前源码目录移除。脚本拒绝覆盖已有组目录。汇总：[command-transport-experiment-20260920.json](command-transport-experiment-20260920.json)。完整证据归档：w5 `/var/lib/arx5-collection/deployments/20260920-command-transport/`，含 60 份 MCAP、收发时间、原始日志、容器配置、实际脚本和 SHA256 清单。

本轮仅完成受控复现实验与记录；没有修改运行代码、调度频率、缓存深度或部署镜像。collection 完整验收及上层 commit 锁定仍待修复后复测；现场长按/释放、录制中 Ctrl+C、双踏板冲突仍未验收。
