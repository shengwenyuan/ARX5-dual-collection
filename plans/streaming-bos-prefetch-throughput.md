# Streaming MCAP → LeRobot 吞吐提升计划

- Status: `proposed`
- Date: `2026-08-26`
- Host: `pi05-cpu`
- Scope: BOS 只读 staging、Episode 级并行转换、有界预取、资源上限与可恢复性
- Non-goal: 修改 MCAP 内容、数据选择语义、gripper 契约或当前正在运行的 run

## 问题定位

当前 `StreamingCoordinator` 使用一个 `ProcessPoolExecutor`，staging 和 conversion 共用同一个 `workers` 上限，且已完成 staging 的 conversion 优先。

这会导致：

1. 启动阶段 BOS 并发读取，随后 conversion 占满 worker，BOS 读取变为间歇式。
2. 无法独立调整 BOS/PFS I/O 并发和 CPU 转换并发。
3. 当 conversion 吞吐足够高时，后续 Episode 可能等待 BOS 复制；当 staging 过快时，又缺少独立的字节级空间边界。

目标不是追求最大进程数，而是让 BOS 持续处于有效顺序读取、conversion 不等待数据，同时确保 PFS 占用不失控。

## 目标架构

```text
Frozen source manifest
        |
        v
bounded stage scheduler --reserve source bytes--> stage executor
        |                                          (BOS -> PFS)
        |                                                |
        +<----------- validated StageReceipt ------------+
                         |
                         v
              bounded ready-stage queue
                         |
                         v
                 conversion executor
                         |
                         v
              committed Episode fragment
```

### 1. 解耦执行器

- staging 使用独立执行器，首选小型 `ThreadPoolExecutor`。复制为大文件顺序 I/O，无需为每个 copy 付出 Python spawn 成本。
- conversion 继续使用 `ProcessPoolExecutor(spawn)`，保持 ROS、PyAV/ffmpeg、LeRobot writer 的进程隔离。
- Coordinator 仍是 manifest 和 Job state 的唯一写者。
- staging 完成顺序不影响最终 LeRobot 顺序；Builder 仍按冻结 manifest 确定性组装。

### 2. 有界预取

同时使用 Episode 数和预留字节数两道硬上限。仅限制 Episode 数不足以约束空间，因为当前单集 MCAP 约为 `7–23.3 GB`。

提交 staging 前使用冻结 `SelectionEntry.mcap.size` 预留完整容量：

```text
can_stage =
  reserved_episode_count + 1 <= prefetch_max_episodes
  and reserved_staging_bytes + candidate.mcap.size <= prefetch_max_bytes
```

`reserved_staging_bytes` 必须包括：

- 正在复制的隐藏临时 staging。
- 已提交、等待 conversion 的 staging。
- 正在 conversion 使用的 staging。
- 因 failed/discarded 而保留的 staging，直到显式处理。

committed/excluded 继续精确删除本 Episode staging 并释放预留。不允许为继续预取而静默删除失败现场；若失败数据耗尽预算，Coordinator 应停止新 staging 并给出明确原因。

### 3. 背压与公平性

- conversion 只要有 `StageReceipt` 且有空闲 conversion slot 就立即执行。
- staging 在未达硬上限时持续补充，不再受 conversion future 数量挤占。
- 首版保持 source manifest 顺序；当队首 Episode 无法放入剩余字节预算时等待空间，不做自动跨越，使调度和恢复更容易审计。
- 可选水位仅用于减少频繁启停：低于 target 时补充，达到 max 时停止。max 始终是硬边界。

## 建议配置模型

新 run 使用 streaming config schema v2：

```toml
schema_version = 2

[runtime]
streaming_root = "/mnt/pfs/swy/dataset/1011/arx5/fold_cloth/streaming"

# 两条独立流水线
stage_workers = 16
conversion_workers = 64

# 软目标与硬上限
prefetch_target_bytes = 1_500_000_000_000
prefetch_max_bytes = 2_000_000_000_000
prefetch_max_episodes = 128
```

兼容性边界：

- 当前 schema v1 及其 `workers` 保留 legacy 读取和 resume 语义，不被新调度默默重解释。
- 当前运行中的 run 不迁移、不修改冻结参数。
- v2 把全部调度参数写入 `run.json`；resume 时必须精确匹配，防止恢复时偷换资源和空间语义。

## 起始值与待对齐上限

下表的“起始值”是实现后的首轮压测值，不是最终性能上限。“候选范围”需要结合实际 cgroup 和存储指标对齐。

| 参数 | 起始值 | 候选范围 | 主要边界 |
| --- | ---: | ---: | --- |
| `stage_workers` | 16 | 8 / 16 / 24 / 32 | BOS 总吞吐平台、I/O wait、PFS 写带宽 |
| `conversion_workers` | 64 | 40 / 64 / 80 / 96 | cgroup CPU quota、CPU throttling、单 worker RSS、SVT 内部线程 |
| `prefetch_target_bytes` | 1.5 TB | 已对齐 | 让 BOS 大批量连续读取，硬上限前留 0.5 TB 弹性 |
| `prefetch_max_bytes` | 2 TB | 已对齐 | 本任务 staging 的行政硬上限，不因 PFS 实际空间较大而放宽 |
| `prefetch_max_episodes` | 128 | 已对齐 | 防止大量小 Episode 绕过字节约束，字节上限仍为主约束 |

### 2026-08-26 资源实测与决策

`pi05-cpu` 当前只读检查：

- CPU quota: `12500000 / 100000 = 125 cores`。
- cpuset: `0-191`，可见 `192` logical CPUs。
- memory limit: `268435456000 bytes = 250 GiB`。
- PID limit: `629145`，file descriptor soft limit: `1048576`。
- `/mnt/pfs/swy` 可用约 `179 TB`，物理空间不是当前限制。
- 正在运行的 `25` 个 conversion worker 合计约使用 `22 cores / 43 GiB RSS`；单 worker 约 `0.85–0.95 core / 1.7–1.8 GiB RSS`。
- 单 worker 可见 `170+` 线程，个别更高。线程多不等于同时占用同样多 CPU，但扩大 worker 后需要观察调度开销。

因此暂不申请更高配额。新调度的 conversion 首轮直接使用 `64 workers`，再比较 `80/96`。只有当 `96 workers` 仍显著提高 Episodes/hour，且 CPU 或内存已接近硬上限，才形成明确的扩容申请依据。

并发递增的停止条件为：相邻档聚合 Episodes/hour 收益低于 `10%`、memory current 超过 `220 GiB`、出现持续 CPU throttling，或 conversion 错误率/PFS 延迟上升。如果 `80 -> 96` 仍有充分收益且上述边界均未触发，可以再短测 `112 workers`，但不直接作为正式默认值。

SVT-AV1 内部会创建编码线程，所以“conversion worker 尽量大”的定义是聚合 Episodes/hour 仍在上升，而不是把进程数直接开到 CPU 或 PID 硬上限。首轮不改 SVT 线程参数；若 `64 -> 80 -> 96` 的扩展效率明显下降，再定向约束 encoder 线程。

PFS 边界冻结为：

- staging 和 streaming 中间产物必须位于 `/mnt/pfs/swy/` 下，不考虑本地 NVMe。
- 本任务 staging 预留总量最多 `2_000_000_000_000 bytes`，在线更改不允许越过该上限。
- 仅在最终 LeRobot 通过验证、snapshot 与 reports 已提交后，自动释放 staging 和 Fragments。
- 失败时保留可 resume 现场，但仍计入 2 TB 上限；不为继续调度而静默删除。

并发上限不应由代码猜测。实施前需读取并记录：

- `cpu.max`/cgroup v1 quota、cpuset、可见 CPU 和实际 throttling 比例。
- cgroup memory limit、单 conversion worker RSS 的 p50/p95/max。
- PFS 剩余空间与本任务 2 TB staging 预算的实时使用量。
- BOS 在 8/16/24/32 个顺序 reader 下的聚合 GB/s 和错误/限流率。
- PFS 同时写 staging、Fragment 和视频时的吞吐与延迟。
- process/file descriptor/PID 上限，以及每个 SVT-AV1 进程的实际线程数。

## 可继续提速的参数层级

后续对齐时把参数分为三类，避免把资源扩容和数据语义变更混在一起。

### A. 只影响吞吐，优先拉高

- staging/conversion 并发度。
- 预取 target/max bytes 和 Episode 上限。
- CPU、RAM、PID/file descriptor 配额。
- staging 位置固定为 `/mnt/pfs/swy/` 下，优化对象只是并发和搬运次数。
- 解码、resize 和 encoder 内部线程数，防止多层过度订阅。
- Builder 的 I/O 路径和临时文件复制放大。

### B. 可能改变性能和产物字节，需要小样本对照验证

- TorchCodec 与 PyAV 解码后端。
- SVT-AV1 `preset`、encoder threads/tile 设置。
- 视频编码的临时缓存和数据搬运方式。
- MCAP clean/select/export 多次扫描的合并或索引复用。

“第二轮 A/B”只是第二阶段小样本对照：固定同一批 `2–4` 个 Episode，基线组使用当前 PyAV + SVT preset 8，候选组每次只改一项，比较 wall time、CPU/RSS、frames 与最终验证。它不是重做 300+ Episode，也不是新建训练数据版本。

第一阶段只做调度与资源提速，保持 PyAV、SVT preset 8、CRF 30 和 MCAP 扫描语义不变。TorchCodec/SVT preset/MCAP 扫描优化不阻塞本次方案，等第一阶段找到新的主瓶颈后再决定是否执行小样本对照。

### C. 可能影响训练语义，默认不为提速修改

- 输出分辨率、fps、帧对齐策略。
- CRF/码率、色彩格式、位深。
- selection tolerance、数据排除规则、gripper 归一化契约。

## 可观测性

每 `5–10 s` 输出一次轻量进度快照，至少包含：

- `stage_active`、`stage_ready`、`convert_active` 和各 Job 终态数量。
- `reserved_staging_bytes`、`failed_staging_bytes`、队列水位。
- BOS 瞬时/滑动平均 GB/s、单 Episode stage s/GB。
- conversion Episodes/hour、frames/s、队列等待时间。
- 整体 CPU、memory、I/O wait、cgroup throttling 和 PFS 可用空间。

性能调整依据是聚合吞吐和 conversion 空转比例，不以单个 worker 速度或瞬时 BOS 带宽为唯一指标。

## 实施顺序

1. 新增 schema v2 参数与严格校验，保留 v1 legacy resume。
2. 将 Coordinator 拆为 stage/convert 两个 executor 和两类 active future。
3. 实现提交前字节预留、Episode/字节硬上限及精确释放。
4. 恢复时校验已存在 staging，重建预留和 ready-stage 队列。
5. 增加状态快照和性能指标，不让调优依赖 tmux 进程猜测。
6. 完成单元、故障注入和 resume 静态验收。
7. 在小批量真实 Episode 上执行 stage/conversion 参数矩阵，再冻结 300+ Episode 正式 profile。

## 验收边界

- 任意时刻 active stage 、ready stage、active conversion 与失败保留数据的总预留不超过字节/Episode 硬上限。
- staging 和 conversion 确实重叠，一类 worker 占满不会阻止另一类的已授权工作。
- source 前后 identity 校验、原子 staging、`stage.json`、Fragment commit 和 Builder 确定性不退化。
- 中断后 resume 可重建队列与字节预留，不重复转换 committed Episode。
- staging/conversion 单独故障不泄漏 worker slot，不破坏其他 Episode。
- 当 PFS 可用空间低于配置安全余量时停止新 staging，已开始的 conversion 可继续排空。
- 运行中 schema v1 run 不受新实现影响。

## 待下一步对齐

1. staging reader 小样本测试的具体执行时机：当前全量 run 完成后，比较 `8/16/24/32` 的聚合 GB/s。
2. conversion 小样本测试的具体执行时机：新调度静态验收后，比较 `64/80/96` 的 Episodes/hour。
3. 第一阶段找到新瓶颈后，再决定是否执行 TorchCodec、SVT preset 或 MCAP 扫描的小样本对照。

BOS reader 短测不改转换产物：从冻结 manifest 选不同的大 Episode，分别以 `8/16/24/32` 并发只读 BOS 并复制到 `/mnt/pfs/swy/` 下的隔离临时目录，记录聚合 GB/s，完成后精确释放该测试目录。相邻档提升低于 `10%` 即认为进入平台，正式值取达到最佳吞吐 `95%` 的最小 reader 数。

## 2026-08-27 凌晨执行策略

- 北京时间 `02:00` 和 `04:00` 各触发一次；两次必须通过 benchmark run-id、metrics 和进程实现幂等，禁止重复启动。
- `02:00` 若旧 schema v1 全量 run 仍活跃，只读记录状态，不发送 tmux 按键、不停止、不部署、不并发压测，留待 `04:00` 再执行。
- `04:00` 若旧 run 仍活跃，低频只读监控并尽量等待结束；无法隔离关键资源时不强行并发。
- 除硬安全边界外，不因耗时、单档失败或普通 warning 提前放弃；高档失败时保留已完成指标，降档继续。
- 硬停止边界：BOS 只读无法保证、写路径逃逸 `/mnt/pfs/swy/`、staging 将超过 `2 TB`、memory current 接近/超过 `220 GiB`、PFS 错误可能损坏其他数据、source identity 变化，或与旧全量任务存在无法隔离的关键资源争用。
- 若旧全量落盘、Builder、validation、manifest 或 loader 验收存在错误，先把现象与只读证据写入 `plans/streaming-current-run-anomaly-2026-08-27.md`，不覆盖/删除旧 run 或旧输出。
- 全量产物错误不自动等价为吞吐测试阻塞；优先使用冻结 source manifest、独立 benchmark run-id、只读 BOS 和隔离 PFS 目录，绕开旧 Builder/最终输出，完成 staging 与 Episode Fragment conversion 吞吐测试。
