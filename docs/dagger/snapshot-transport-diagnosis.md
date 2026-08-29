# DAgger Snapshot 传输诊断

- Status: `root cause isolated; local IPC repair accepted on W3`
- Date: 2026-08-28
- Scope: 三路 RGB Snapshot、rosbag2 并发负载与 Fast DDS/SHM

## 固定边界

- D405 主线继续发布并录制三路 `848×480 RGB8 + Z16 @30 Hz`。
- Snapshot 只返回同一真实因果组的三路 `640×360 RGB8`，不插值、不补帧、不修改源时间戳。
- Snapshot hard timeout 保持 `200 ms`；RTC request 保持 `350 ms`，另保留 `50 ms` margin。
- Shadow 不发布动作，失败只降低 `shadow.quality`，不改变 Episode outcome。
- 诊断不得发送机械臂控制指令，也不得通过放宽 timeout 获得通过结论。

## 已完成对照

| Case | Recorder | Snapshot payload | SHM segment | port queue | 结果 |
|---|---:|---:|---:|---:|---|
| 640×360 首轮 | 开 | 2.07 MB | 64 MiB | 2048 | 330 次中 12 次 timeout；常态延迟明显改善，但未通过零超时门槛 |
| 无录制 Probe | 关 | 2.07 MB | 64 MiB | 2048 | 服务就绪后 399/399 成功，p99 37.7 ms |
| SHM 128 长时 | 开 | 2.07 MB | 128 MiB | 2048 | 196 s，490 次中 62 次 timeout |
| SHM 128 + I/O Probe | 开 | 2.07 MB | 128 MiB | 2048 | 192.5 s，481 次中 92 次 timeout；约 120 s 开始成簇失败 |
| queue 8192 | 开 | 2.07 MB | 128 MiB | 8192 | 143.1 s，358 次中 82 次 timeout；约 96.8 s 开始成簇失败 |

`640×360 RGB8` 三图 payload 为 `2,073,600 bytes`；此前 `848×480 RGB8` 为
`3,663,360 bytes`，下降约 43.4%。该优化保留：它降低了正常请求延迟，且不影响 MCAP。

## queue 8192 最终观测

- Episode 正常结束，MCAP 约 26.27 GB，时长 143.05 s。
- 358 次 inference 中：263 success、13 recovered、82 buffers_not_ready。
- 排除启动瞬间一次失败，首个真实 timeout 位于 96.83 s；之后形成 13 段连续失败，单段最长约 5.6 s。
- 成功请求：Snapshot p99 36.6 ms、Policy round-trip p99 94.9 ms、Client total p99 122.8 ms。
- C++ 没有 `>=50 ms` 的 select/resize/response-build callback。
- `/dev/shm` 为 2 GiB，监控使用量最大约 6.6 MiB；Container RSS 最大约 1 GiB，主机内存无压力。
- NVMe 持续约 180–188 MB/s，低 util、无 iowait；Dirty pages 平稳，没有与 timeout 同步的突变。

## 已排除项

当前证据足以排除以下直接根因：

- C++ 因果配组或 RGB resize 计算过慢；
- Policy Server inference 过慢；
- NVMe 写盘或内核 writeback 阻塞；
- `/dev/shm` 挂载容量耗尽；
- Fast DDS `segment_size=64 MiB` 不足；
- Fast DDS `port_queue_capacity=2048` 不足；
- Python timeout future 不清理：Jazzy `rclpy` 在 `future.cancel()` 后会移除 pending request。

因此不再继续扩大 SHM segment、queue capacity 或 `/dev/shm`。这些变量回到生产基线：

- `segment_size=64 MiB`
- `port_queue_capacity=2048`
- Collector `/dev/shm=1 GiB`

## 当前定位

故障位于 C++ callback 完成之后、Python future 完成之前：

```text
C++ select/resize/response build
  -> DDS service response 序列化与发送
  -> Fast DDS transport / listener
  -> Python rclpy future 完成
```

正常响应只需约 3–39 ms；异常响应直接越过 200 ms，呈成簇、二态分布，而非随 CPU、磁盘或模型计算逐渐变慢。Recorder 是必要触发条件，当前最可能是六路大图持续录制时，约 2.07 MB Service response 在 DDS 数据面出现资源竞争或调度长尾。

## 最后两项 A/B

### A. 极小响应负载：通过

保持 D405、八路 MCAP、请求频率、因果配组与 Recorder 负载不变，仅让实验版 Snapshot Service 返回极小图像 payload。连续执行不少于 180 s。

目的：判断故障是否由 Service response 的大 payload 触发。实验客户端只验证 ready、时间戳与响应完成，不进入 Policy，也不产生动作。

2026-08-28 W3 实测：八路 Recorder 连续运行约180秒，450/450次请求成功；p50 2.10 ms、p95 2.36 ms、p99 2.62 ms、max 4.86 ms。MCAP约33.1 GB，Recorder报告12条transport-layer lost message，说明高带宽录制负载真实存在，但极小Snapshot响应没有任何timeout。

结论：因果选择、Service生命周期和Python executor本身可稳定运行；故障明确绑定于大型Service response的数据传输，而不是仅由Recorder进程存在触发。

### B. Fast DDS asynchronous publication：不通过

恢复真实三路 `640×360 RGB8` response，保持其余负载不变，只将 Fast DDS publication mode 改为 asynchronous。连续执行不少于 180 s。

目的：判断同步 publication 在大图 Publisher 与 Service response 共用 Participant 时是否造成响应发送长尾。

2026-08-28 W3 实测：真实三路 `640×360 RGB8`、八路Recorder连续运行约180秒，432/450次成功、18次timeout；成功请求p50 5.63 ms、p95 26.59 ms、p99 35.93 ms、max 46.61 ms。

失败约每20秒形成2–3次短簇，没有同步模式约100–120秒后的持续恶化，但仍未满足零timeout。因此asynchronous publication只能改变故障形态，不能作为生产修复。

下一轮继续拆分两个问题：

1. 使用固定Recorder负载与同步publication，对Snapshot响应尺寸做阶梯测试，定位payload阈值。
2. 使用真实`640×360`响应做UDP-only对照，判断故障是否为Fast DDS SHM特有。

### C. Payload阶梯与UDP-only：问题继续上移

同一同步publication、SHM基线、八路Recorder、450次/180秒条件下：

| Snapshot尺寸 | 三图payload | 结果 | 首次失败 |
|---|---:|---:|---:|
| `320×180` | 0.52 MB | 450/450成功，p99 6.03 ms | 无 |
| `400×225` | 0.81 MB | 356/450成功 | index 308，约123.2 s |
| `480×270` | 1.17 MB | 363/450成功 | index 289，约115.6 s |

真实`640×360`、2.07 MB响应改为UDP-only后，408/450成功、42次timeout，首次失败位于index 346、约138.4秒；此后失败持续增加。UDP改变了恶化速度，但没有消除故障。

结论：

- 大响应存在明确payload相关边界，当前稳定区间在0.52–0.81 MB之间。
- 故障可跨SHM与UDP复现，不是Fast DDS SHM transport专有问题。
- 下一决定性对照是把Snapshot Client从内嵌`rosbag2_py.Recorder`的Python进程中隔离到独立进程；用来区分客户端进程内竞争与服务端/DDS公共路径。

### D. Snapshot Client进程隔离：客户端已排除

保持SHM基线、真实`640×360`响应和八路Recorder，只把Snapshot Client放入独立Python子进程。结果为358/450成功、92次timeout；首次失败位于index 287、约114.8秒，之后持续恶化。

结论：故障与Client和`rosbag2_py.Recorder`是否同进程、Python GIL或Client executor归属无关。当前边界缩小为：高带宽Recorder订阅使C++ D405 Source Participant中的可靠Camera writers与大型Service response writer发生公共DDS资源竞争。

下一对照减少Recorder订阅的数据面，但保持Source与真实Snapshot不变，用来验证竞争是否随被录制的可靠图像流量变化。

### E. Recorder负载消融：根因边界确认

保持C++ Source仍发布完整六路`848×480 RGB-D@30`，保持真实三路`640×360` Snapshot response，只让Recorder订阅三路Color和双臂，不订阅三路Depth。结果450/450成功，p50 5.45 ms、p95 26.40 ms、p99 37.30 ms、max 47.10 ms。

全RGB-D Recorder约183 MB/s时稳定复现；仅Color Recorder约110 MB/s时完全通过。结合以下事实：

- 极小response在全RGB-D Recorder下通过；
- 真实response在无Recorder或Color-only Recorder下通过；
- SHM、UDP、同步、异步和独立Client进程均不能在全负载下稳定通过；
- Camera Topics当前为`reliable keep_last(2)`，Service为reliable；两者属于同一个D405 Source Participant；

根因边界确定为：**六路可靠大图在全量Recorder订阅下占用D405 Source Participant的DDS可靠发送资源，使同Participant内大型Service response产生长期运行后的传输饥饿/长尾。**

本项目不能通过删除Depth、改为best-effort、放宽timeout或降低25 Hz标准规避。最终修复应保留小型Service的请求/因果语义，但把三图payload移出DDS：C++写入有generation校验的双缓冲共享内存，Python收到小响应后读取对应slot。Canonical Topics、可靠QoS、Recorder和MCAP完全不变。

## 后续决策

```text
极小响应：通过
├─ asynchronous publication：不通过
├─ UDP-only：不通过
├─ Client独立进程：不通过
└─ Color-only Recorder：通过
   └─ 进入小型Service + 双缓冲共享内存实现
```

共享内存候选不得改变 Canonical Topics、MCAP、Depth、Snapshot 因果标准或 Policy 协议。它只替换约 2.07 MB 图像从 C++ Source 到本机 Python Client 的传输方式。

## 共享内存修复验收

第一阶段实现保留小型ROS Service作为因果请求和descriptor通道；C++把同一因果组三路`640×360 RGB8`写入带generation校验的双缓冲arena，Python只读复制。大型图像不再经过DDS Service；Canonical Topics、完整RGB-D Recorder与MCAP均未改变。

W3无动作打桩验收结果：

| Case | Recorder | 请求频率 | 结果 | Snapshot延迟 |
|---|---|---:|---:|---:|
| 修复后基准 | 完整八路 | 2.5 Hz | 450/450，零失败 | p99 5.05 ms，max 5.18 ms |
| 修复后加压 | 完整八路 | 5 Hz | 1000/1000，零失败 | p99 5.17 ms，max 5.95 ms |

第二轮持续200.42秒，MCAP约34.3 GiB；左右臂分别200369/200355条，六路相机分别6006–6010条，均维持目标频率。Recorder报告5条transport-layer lost message；这是完整RGB-D主线本身的既有监督项，未造成Snapshot失败，也不通过插值或伪造修补。

`25 Hz`是Action Gateway执行频率；本profile每10步预取一次，因此Snapshot实际基准请求频率为`25/10=2.5 Hz`。5 Hz加压已经覆盖两倍生产请求率，不能把两者混同后要求Snapshot无意义地每个控制tick重新取图。

继续重复后，第一阶段仍复现两个各5次的DDS Service timeout簇：一次为5/450，一次为5/1000。带逐请求序号的隔离诊断证明，超时请求全部进入C++ callback且均约3 ms完成；1001次callback p99约3.80 ms、max 10.01 ms，没有缺失或卡住。也就是说，图像共享内存、因果选择、resize和commit已经稳定，残余长尾唯一位于C++ callback完成后的DDS小响应到Python future之间。

因此最终边界进一步收紧：不能把DDS Service继续留作Snapshot控制面。最终修复改用容器内Unix domain socket传递单字节请求与32-byte结果，图像和完整observation metadata仍通过generation保护的共享内存读取。该路径不改变ROS Canonical Topics、MCAP、Depth或Policy协议。

### 最终本地IPC验收

| Case | 请求频率 | 时长 | 结果 | Snapshot延迟 |
|---|---:|---:|---:|---:|
| 生产频率 | 2.5 Hz | 180.09 s | 450/450 | p99 3.70 ms，max 3.81 ms |
| 两倍加压 | 5 Hz | 200.15 s | 1000/1000 | p99 3.98 ms，max 4.09 ms |
| 长时加压 | 5 Hz | 400.24 s | 2000/2000 | p99 4.15 ms，max 7.34 ms |

最长一轮MCAP约68.4 GiB：左右臂400048/400116条，六路相机11986–11998条；完整RGB-D Recorder持续运行。Recorder报告3条transport-layer lost message，继续作为MCAP质量监督项，不影响Snapshot结论，也没有插值或伪造。

最终代码不包含概率日志、payload消融、DDS参数消融或测试Harness；Fast DDS与容器SHM配置保持生产基线。Snapshot IPC由独立`SnapshotSocketServer`、`SnapshotSharedMemoryWriter`和`LocalVlaSnapshotClient`组成。

## 真实策略链路复验

2026-08-29 W3 使用折叠衣物RTC checkpoint完成Shadow与Take-over验证：

- Shadow三条Episode共661次推理全部成功，Snapshot p99不超过4.52 ms、最大6.85 ms。
- Take-over长Episode内417次推理提交、415次接受；2次随控制epoch切换或最终fault作废。RTC往返p99为110.88 ms，控制队列最低4步，无underrun。
- 后续success Episode为96/96次接受，RTC往返p99为104.96 ms，控制队列最低7步。
- 全程没有Snapshot timeout、`buffers_not_ready`或IPC错误。唯一`FAULT_HOLD`来自模型右夹爪归一化值`-0.002147`超出`[0,1]`，不属于通信问题。

结论：本地IPC不仅通过无动作压力测试，也已通过真实Policy与Take-over运行；Snapshot transport问题收口。
