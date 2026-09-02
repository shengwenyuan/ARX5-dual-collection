# Human-Gated DAgger 需求

- Status: `aligned`
- Updated: 2026-08-20
- Scope: 本地 VLA 推理、显式人工接管、连续 MCAP 与离线训练区间

## 目标

在普通人工采集之外提供独立 DAgger 模式。模型从真实设备状态开始执行；操作者可显式接管，从模型实际到达的状态继续纠正，再显式交还控制权。同一 Episode 可以多次交替，设备和模型在多条 Episode 之间保持长生命周期。

首个模型实现是 π0.5，但 Collector 与 Policy Server 的边界面向后续 VLA，不把 π0.5 数据布局写进采集状态机。

## 冻结边界

- 普通 `arx5-collect run` 不获得模型控制权限；DAgger 使用独立入口。
- 当前硬件没有六维力传感器，不以关节电流推断人工意图。
- 右踏板，即 Station `activate`，负责 Episode 开始和 success 结束。
- 左踏板，即 Station `abort`，在 DAgger 中只负责模型/人工所有权切换；Shadow 中明确忽略。
- DAgger Abort 保留键盘、UI、`Ctrl+C`、物理急停和系统故障入口，不占用所有权踏板。
- 原始 MCAP 从 Episode 开始到结束连续记录，不因接管切段、暂停、裁剪或覆盖。
- 不寻找或伪造“模型开始出错”的时刻，不回退到历史状态另录演示。
- checkpoint 是配置变量；Episode metadata 记录 SHA-256、介入次数和控制区间。
- metadata 不记录两个 Container 版本或协议版本。

## 数据契约

主线 MCAP 的感知真值仍是六路 RGB-D 与双臂 ArmState。模型输入来自这些实时 Topic，但不额外写入“模型实际输入”、低清图像或逐次 inference 源消息引用。

DAgger 只增加一个稀疏事件 Topic：

```text
/dagger/authority
```

它只记录会改变数据语义或控制安全的状态：

1. `TAKEOVER_REQUESTED`：操作者请求接管。
2. `HUMAN_ACTIVE`：模型 gate 已关闭，待执行 action 已清空，双臂确认可由人工操作。
3. `RESUME_REQUESTED`：操作者声明专家纠正结束。
4. `POLICY_ACTIVE`：新 observation 与新 action 已就绪，模型重新取得控制权。
5. `FAULT_HOLD`：故障导致模型不能继续控制。

事件包含递增 sequence、intervention ID、control epoch、ROS 时间、单调时钟和原因，不包含图像、ArmState 或 inference 引用。

专家训练区间严格定义为：

```text
HUMAN_ACTIVE -> RESUME_REQUESTED
```

`TAKEOVER_REQUESTED -> HUMAN_ACTIVE` 是安全交接区间；`RESUME_REQUESTED -> POLICY_ACTIVE` 是重新推理和恢复区间。两者都不得作为专家 action loss。离线 pipeline 可以读取接管前后的原始上下文，但不得把上下文中的模型动作标成专家动作，避免 leakage 或 oracle。

普通推理请求、成功、延迟和失败不进入 MCAP。Shadow 的逐次诊断写入 Session JSONL；Episode metadata 只保留汇总质量。

## Server / Client

```text
config/runner/compose.dagger.yaml
├─ policy-server
│  ├─ openpi / JAX / CUDA / checkpoint
│  ├─ PI 风格 request/response
│  └─ 无 ROS、CAN、相机和 Recorder 权限
└─ collector
   ├─ 统一 C++ 三 D405 Source
   ├─ 进程内 VlaSnapshot endpoint
   ├─ AsyncPolicyClient
   ├─ Episode / Recorder / metadata
   └─ TakeoverController（D1 后实现）
```

- Policy Server 只加载模型和返回 action proposal。
- Collector 独占相机、ROS、CAN、控制权和录制链路。
- 两个 Container 由一个 Compose 统一启动；Policy Server 不使用 privileged。
- Policy Server 每个 Session 只加载一次 checkpoint，并与 Collector 配置的 SHA-256 对齐。
- PI 风格 observation 只包含命名三相机图像、双臂 state 和 prompt；关联 envelope 包含 session、episode、epoch 和 request ID。
- 迟到或旧 epoch 响应必须作废。
- Client 接口必须异步；Shadow 只消费结果和记录诊断，不执行 action。

## Observation

生产相机改为统一 `rclcpp + librealsense C++` Source。W3 已证明 Python 与独立 C++ 高带宽 Snapshot Subscriber 在真机录制并发下都会产生秒级 callback backlog，而同一 MCAP 原始数据持续满足 40/2 ms。因此 Snapshot 不再二次订阅三路 Color：

- 一个进程打开三颗 D405，每颗相机使用独立 Pipeline、采集线程、SDK capacity-1 queue 和单机 Depth 对齐。
- 同一份 Color message 直接进入进程内小历史并发布主线 Topic；Snapshot 不经过第二次 ROS/DDS 图像入口。
- 双臂 ArmState 仍通过轻量 ROS subscription 进入同一进程。
- C++ 选择最新完整严格因果 snapshot，通过内部 `/dagger/get_snapshot` service 返回原始 YUYV 与 ArmState。
- Python inference worker 按需请求 snapshot，再完成颜色转换、缩放和模型字段编码。
- 三相机只使用真实 Header 时间，允许时间邻近选择，不插值、不补帧、不重复伪造。
- 双臂状态必须不晚于相机 cutoff。
- 选择失败立即返回 `observation_unavailable`，不等待下一帧，不复用上次 observation。
- YUYV→RGB 与 transport resize 在 worker 中执行；模型 normalization、layout 和 action postprocess 属于 Policy Server adapter。

三颗 D405 不支持多机硬同步，启动相位不是稳定约束。相机组以 overview 为锚点，left/right 选择时间最近的真实帧；三帧最大跨度不超过 40 ms，即一个 30 Hz 帧周期加有限调度抖动。该选择不插值、不补帧、不伪造数据。ArmState 相对 cutoff 年龄不超过 2 ms，snapshot 年龄不超过 100 ms。

上述门槛必须显式写入 Collector 配置。选择失败按 `buffers_not_ready`、`camera_span_exceeded`、`snapshot_stale`、`left_arm_stale` 或 `right_arm_stale` 分类，并把实际值与门槛写入 Session JSONL；不得只输出无法定位原因的通用错误。

该 C++ Source 不承担模型协议、Recorder、MCAP 事件或控制权。普通生产与 DAgger 共用相机 Source；只有 DAgger 启用 Snapshot service。

## Shadow 与 Take-over

Shadow 和 Take-over 是两个状态机，只共享 Observation、异步 Policy Client、配置和 metadata 契约。

### D0 Shadow

- 无 command lease、无 Gateway、无模型动作下发。
- 左踏板明确忽略；右踏板正常开始和结束 Episode。
- observation 或 inference 失败只使 Shadow 质量降级，不中止 Episode。
- 单次只允许一个在途 request；下一次调度不会阻塞 Episode 主线程。
- 每次尝试写 Session JSONL；成功不得产生 MCAP action Topic。

### D1 Take-over

Take-over 必须新建 `TakeoverController`，不得从 ShadowMonitor 继续堆叠。状态至少包含：

```text
MODEL_CONTROL
HANDOVER_PENDING
HUMAN_ACTIVE
RESUME_PENDING
FAULT_HOLD
```

控制顺序：

```text
接管：左踏板 -> 关闭 gate -> epoch++ -> 清空 action -> 双臂 G_COMPENSATION -> HUMAN_ACTIVE
恢复：左踏板 -> RESUME_REQUESTED -> fresh observation -> fresh action -> 安全检查 -> POLICY_ACTIVE
```

在新 action 就绪前不能恢复模型。Observation、Policy、Gateway 或控制器确认失败必须进入 `FAULT_HOLD`；绝不继续旧 action。

Gateway 是模型命令到 ARX Vendor Controller 的唯一入口，持有双臂统一 command lease、control epoch、watchdog 和安全检查。首版冻结为：

- Policy response 是 robot-space absolute `14D`：左六关节 rad、左 normalized gripper、右六关节 rad、右 normalized gripper。
- normalized gripper 只接受 `[0,1]`，再按 Station calibration 映射为 Vendor raw；越界 reject，不 clamp。
- 相邻 action 最大 joint step 为 `0.25 rad`，相对 ticket 验证时实际状态的最大 departure 为 `1.5 rad`。
- 当前 v2 profile 为 50-step chunk、执行前 10 步、25 Hz。10 步结束后才以新 observation 请求下一 chunk，间隙保持最后 Vendor target。
- Gateway 只接受不超过 `0.1 s` 的 canonical 双臂实际状态；该阈值与执行参数一样由模型 profile 配置。
- Policy 等待最多 `0.5 s`；有待执行 action 时 command deadline 为 `0.12 s`。超限与 Observation、Policy、安全检查或 Publisher 故障均 fail closed，进入 `G_COMPENSATION + FAULT_HOLD`，并以 fault 事件时刻立即结束当前 Episode。该 Episode 以 `outcome=fail` 落入 `dagger_fail/`，Session 回到 `READY` 等待右踏板；相机、CAN、ROS 等共享链路故障仍进入 `SESSION BLOCKED`。
- Vendor `remote_slave` command callback 默认物理锁死；只有 fresh action 完整通过后，Gateway 才同时确认双臂 `enable_policy_control`。GO_HOME、人工接管、Episode 结束和任何 fault 都重新锁死。

## metadata

所有新 Episode 顶层写 `collection_type`：普通采集为 `demonstration`，DAgger 为 `dagger`。DAgger metadata 只增加：

- `checkpoint_sha256`
- `intervention_count`
- `control_segments`
- Shadow 模式下的汇总质量、尝试数、成功数、失败数和恢复数

控制区间由 `/dagger/authority` 事件确定。原始 MCAP 不因离线处理改变；清洗只输出索引、质量和 loss mask。

### 离线裁切与 LeRobot 边界

原始 MCAP 不做物理裁切。离线 pipeline 必须先验证事件与 metadata，再生成半开区间索引：

1. 将每条 authority 事件匹配到对应的 `control_segments` 边界；计算 `event.monotonic_time_ns - boundary_offset_ns`。同一 Episode 的所有结果必须得到同一个单调时钟 anchor，否则该 Episode 不得进入训练集。
2. 使用 authority 消息的 MCAP `bag_timestamp_ns` 与对应 offset 估计 Episode 的 bag-time anchor；所有训练区间转换为 `[start_ns, end_ns)`。不得用 MCAP 第一条或最后一条消息替代 Episode 语义边界。
3. DAgger 专家数据只来自 `owner=human`，即 `HUMAN_ACTIVE -> RESUME_REQUESTED`。handover/resume pending、model、fault 和 Episode 停止后的 Recorder 清理尾部均不产生专家 loss。
4. 以 overview frame group 的 `bag_timestamp_ns` 判断 observation tick 是否落在区间内；图像和状态仍按既有 Header 时间执行严格因果选择，禁止未来帧、插值和补帧。
5. action target 的全部时间步必须落在同一个 human 区间。靠近区间末端而无法提供完整 horizon 的样本直接丢弃，不跨越 `RESUME_REQUESTED`，不 pad、不复用最后 action。
6. 每个 human 区间导出为独立 LeRobot episode。来自同一个原始 Episode 的全部派生片段必须进入同一 train/validation split，避免跨 split leakage。

普通 `collection_type=demonstration` 不读取 authority；其有效域仍为 Episode `[0, duration_s)`，再执行既有质量和 selector 规则。混合数据集必须在外部 manifest 保留 `collection_type`、source Episode、intervention ID 与来源区间，不把 DAgger model 区间误标为人工演示。

## 分阶段验收

1. D0 静态：C++ 进程内因果选择、异步单在途 Client、旧 epoch 作废、Shadow 故障隔离。
2. D0 真机：W3 首轮连续 45–60 秒 Shadow；八路原始流保持生产基线，配组/推理成功率不低于 95%，不得连续超过 2 秒 Observation 选择失败，Sampler cache 不落后原始 Topic，Episode 正常结束，左踏板无影响。稳定后再恢复 90–150 秒常规验收。
3. D1 无动作：TakeoverController、authority 事件和 metadata 以 fake Gateway 验收。
4. D1 真机：由用户启动和操控，先验证单次接管，再验证多次交替；未经单独批准不得发送控制命令。
