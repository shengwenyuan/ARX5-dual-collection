# `/infer/command` 第二轮定位：累计序号与 Fast DDS 进程内交付

2026-09-20，w5，无模型/相机/机械臂启动。接续 [首轮 A/B/C](command-transport-experiment-20260920.md)。**本轮新增 160 局、发送 60,000 条，已把故障定位到 Fast DDS 2.14.6 的新 reader 初始序号处理与进程内可靠交付组合。仅重建 publisher 即可通过当前合成回归，无需重建 node/context。尚未修改生产实现。**

后续：[共享按局 publisher 修复](publisher-lifecycle-fix-20260920.md)已完成，infer 与 DAgger 均通过无真机回归；本文保留定位阶段记录。

## 实验结果

| 组别 | 条件 | 完整局 / 总局 | 回读 / 发送 | 首次异常 |
| --- | --- | ---: | ---: | --- |
| P200 | 持续 publisher，每局 200 条 | 20/20 | 4,000 / 4,000 | 未到阈值；随后另起 40 局长测 |
| P200x40 | 同条件，在一个进程内连续 40 局 | 25/40 | 5,000 / 8,000 | 第 26 局；后续每局 200 条全部未录入 |
| P800 | 持续 publisher，每局 800 条 | 7/20 | 15,987 / 16,000 | 第 8 局；每局缺 step 0，约 5.10 秒积压 |
| W400 | 每局只重建 publisher，保留 node/context | **20/20** | **8,000 / 8,000** | 无；最大接收延迟 0.456 ms |
| TRACE400 | 原始持续 publisher，加入只读取证函数包装 | 13/20 | 7,993 / 8,000 | 第 14 局；抓到首条被拒绝的底层返回值 |
| FULL400 | 持续 publisher，显式启用进程内交付 | 13/20 | 7,993 / 8,000 | 第 14 局；相同缺首条/积压特征 |
| OFF400 | 与 FULL400 相同，只关闭进程内交付 | **20/20** | **8,000 / 8,000** | 无；最大接收延迟 0.598 ms |

每局仍为 50 Hz、RELIABLE、publisher depth 256，使用已安装的消息格式和 Recorder；不增加探测消息或 ACK 等待。所有组均显式等待本局匹配，复用首轮的 0.2 秒 bootstrap、0.1 秒尾部、1 秒局间间隔。W400 从第二局起只 `destroy_publisher/create_publisher`，保留原 node/context、相同 QoS。

P200x40 是一个新的连续 40 局进程，不把 P200 的 20 局与重启后的局数拼接。不同组在独立 network/IPC 容器中部分并行，ROS domain 177–181，无硬件/GPU映射；共用 w5 CPU，因此微秒级性能比较不作为结论。明确的丢失、5 秒积压、累计序号阈值和底层拒绝原因是本轮判据。用户已有 policy-server 保持运行。

## 已确认的因果链

1. 持续 publisher 的 DDS 序号跨 episode 累加；新 Recorder 的 reader 刚建立时，历史接收边界尚未正确推进到当前 writer 序号。匹配状态本身不能保证这个边界已经初始化。
2. 第 14 局第一条对应 DDS sequence **5201**。直接包装当前安装库的 `WriterProxy::unknown_missing_changes_up_to`，观察到返回 **5200**，即把过去局的序号也算进“未知缺失”。
3. `DataReaderHistory::received_change` 随即返回 **false**，`rejection_reason=2`，对应 **`REJECTED_BY_SAMPLES_LIMIT`**。当前库默认 `max_samples=5000`，与源码条件 `history.size() + unknown_missing < max_samples` 一致。这不是 MCAP 写盘失败，也不是发送端漏发。
4. `StatefulWriter::intraprocess_delivery` 返回 false。当前版本进程内路径不会像普通远端可靠传输那样对这一拒绝正常走补发；后续已收到的数据仍等待缺失序号。源码中数据交付先于进程内 heartbeat，读者的同进程 heartbeat 路径不启动常规重传请求。
5. 缺失首条仍占 writer 历史，直到发布 step 256 时触及 depth 256 的淘汰。对应源码通过 GAP 推进缺失边界，积压数据随后进入 Recorder。7 个取证异常局中，step 1 的实际接收均发生于 step 256 发送后 **0.104–0.261 ms**；step 0 始终缺失。

第 14–20 局逐局抓到 `sequence=5201,5601,...,7601`、`unknown_missing=5200,5600,...,7600`，全部因 samples limit 被拒绝。拒绝发生在各局 step 0 发送后 0.124–0.299 ms，早于 step 1 发送，已与 `sent.json` 和 MCAP 缺失对应。

原真机 19 局也吻合：第 13 局开始前累计 4,804 条，录制完整；第 14 局开始前累计 5,194 条，首次缺 step 0；其后 5 局均已超过阈值且同样缺首条。这里使用既有 metadata 发送计数交叉核对，不是给原始数据补写 DDS 序号。

这解释了局长对照：200 条/局的首个坏局为 26（此前 5,000 条）；400 条/局为 14（此前 5,200 条）；800 条/局为 8（此前 5,600 条）。**触发条件是新 reader 接入时面对的累计序号，而非固定局数。** 200 条不足以挤掉 depth 256 中的首条，所以录制停止前整局无法交付。

证据层次：底层函数返回、拒绝类别、DDS 序号、丢失与交付时间为直接实测；初始化/heartbeat/GAP 的内部完整分支由对应版本源码解释和 FULL/OFF 对照支持。本轮未修改 Fast DDS 源码、未声称验证了上游补丁，也未把首条拒绝描述为网络丢包。

## 与资源泄漏、配置的区别

- 各局停止后的线程数保持 15，文件句柄通常 15（取证组多 1 个日志句柄），SHM 文件数保持 10；没有观察到随局数增加的这些资源计数。RSS 有小幅增长，不能据此证明绝无内存泄漏，但本故障已有更直接的拒绝原因。
- 发现一个独立配置接入问题：镜像只设置 `FASTDDS_DEFAULT_PROFILES_FILE`；当前 Fast DDS 2.14.6 加载器识别的是 **`FASTRTPS_DEFAULT_PROFILES_FILE`**。用安装库和头文件编译只读 profile 检查器验证：只设置前者时，指定 XML 不生效，实际是 builtin transports、进程内 FULL；设置后者后才读到配置中的 64 MiB SHM segment 和 queue capacity 2048。
- 因此前轮基线里的 XML 路径只是环境声明，不能视为其参数已生效。本轮 FULL/OFF 两组都显式加载同一套 transport 配置，实际配置读回一致，唯一交付设置差异为 FULL/OFF。FULL 仍复现，OFF 通过，因此没有把 transport 配置是否加载的差异误当成关闭进程内交付的效果。
- 此处接收端 `max_samples=5000`、命令发布端 `depth=256`、SHM `port_queue_capacity` 是不同参数；本次不是 SHM 队列 2048 用满。进程内直接交付绕过常规 transport。

## 建议的最小生产修复

优先按 episode 重建 **`/infer/command` publisher 实体**，保留 context/node、消息契约、现有 Recorder、RTC 调度及 50 Hz。W400 已证明不必完整关闭/重开 ROS context。每局仍在首条发送前完成匹配，并在 Recorder 结束、动作发送关闭后处理发布端生命周期。

全局关闭 Fast DDS 进程内交付是已验证的另一条规避路径，但影响面覆盖其他 ROS 通信，暂不作为首选生产改动。配置变量兼容性问题单独修正和验证，不与 publisher 生命周期修复混成一个无法归因的变更。提高资源上限只会推迟累计序号触发点，不作为修复建议。

本轮只落盘实验脚本/结果/定位文档，未修改采集运行代码、生产 XML、模型或镜像。生产接线后仍须通过真实入口的无真机连续回归，再由用户做现场采集验收；原 6 个缺首动作的真实 episode 仍按坏数据整局隔离，不能因定位成功而恢复为合格。

## 复现与证据

本轮冻结脚本现仅保留于 w5 `20260920-command-transport-root-cause/scripts/` 历史归档：`infer_command_transport.py` 与 `infer_command_transport_probe.py`。其他文件也在该归档目录：`fastdds_history_probe.cpp` 为版本限定、仅转调原函数并记录结果的诊断包装；`fastdds_profile_probe.cpp` 只读 XML/default QoS；两份 XML 仅用于隔离测试。

在 w5 上，先将上述历史归档 scripts 目录复制为 `$PROBE_SCRIPTS`，输出选择新目录：

```bash
PROBE_SCRIPTS=/tmp/arx5-command-probe-scripts
PROBE_RESULTS=/tmp/arx5-command-probe-results-new
mkdir -p "$PROBE_RESULTS"
docker run --rm --name arx5-probe-repro --network none --shm-size 1g \
  -e ROS_DOMAIN_ID=177 -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint /ros_entrypoint.sh \
  --mount "type=bind,src=$PROBE_SCRIPTS,dst=/test,readonly" \
  --mount "type=bind,src=$PROBE_RESULTS,dst=/results" \
  sha256:ed5e7447389de7b383aab0c076507a292062e68dbe01803c5959dc62ec90db8c \
  python3 /test/infer_command_transport_probe.py \
  --messages 200 --episodes 40 --output /results/P200x40
```

其他组分别使用：P800 `--messages 800 --episodes 20`；W400 `--messages 400 --episodes 20 --lifecycle publisher`。FULL/OFF 使用默认 400 条/20 局，额外将 `FASTRTPS_DEFAULT_PROFILES_FILE` 和 `FASTDDS_DEFAULT_PROFILES_FILE` 同时指向 `/test/fastdds-intraprocess-full.xml` 或 `...-off.xml`。每组换新的容器名与输出目录，保持独立 IPC/network。

取证 `.so` 用现有镜像的编译器生成，不安装依赖：

```bash
g++ -std=c++17 -shared -fPIC -O2 /test/fastdds_history_probe.cpp \
  -o /results/fastdds_history_probe.so -ldl
```

随后仅在取证容器设置 `LD_PRELOAD=/results/fastdds_history_probe.so`、`DDS_PROBE_LOG=/results/TRACE400-dds.jsonl`，运行默认 20 局。包装只适用于本次已核对的 Fast DDS 2.14.6 ABI，不用于生产。日志中低序号 discovery 调用不作为命令证据；汇总仅匹配 `history_received` 的高序号首条拒绝，再与逐局发送时间关联。

逐局汇总：[command-transport-root-cause-20260920.json](command-transport-root-cause-20260920.json)。完整归档：w5 `/var/lib/arx5-collection/deployments/20260920-command-transport-root-cause/`，含 160 份 MCAP、收发明细、底层取证日志、实际脚本/编译产物、容器隔离配置、profile 读回、对应上游源码快照与 SHA256 清单。测试过程中未重建镜像或下载运行组件；仅读取上游源文件以解释当前安装版本。

源码依据（Fast DDS v2.14.6）：[接收端 samples limit 判定](https://github.com/eProsima/Fast-DDS/blob/v2.14.6/src/cpp/fastdds/subscriber/history/DataReaderHistory.cpp#L196)、[默认 resource limits](https://github.com/eProsima/Fast-DDS/blob/v2.14.6/include/fastdds/dds/core/policy/QosPolicies.hpp#L1764)、[进程内交付与 heartbeat 顺序](https://github.com/eProsima/Fast-DDS/blob/v2.14.6/src/cpp/rtps/writer/StatefulWriter.cpp#L621)、[ReaderProxy 淘汰后的 GAP](https://github.com/eProsima/Fast-DDS/blob/v2.14.6/src/cpp/rtps/writer/ReaderProxy.cpp#L630)、[WriterProxy heartbeat 分支](https://github.com/eProsima/Fast-DDS/blob/v2.14.6/src/cpp/rtps/reader/WriterProxy.cpp#L563)。
