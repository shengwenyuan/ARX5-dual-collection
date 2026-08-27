# Streaming MCAP → LeRobot 第二轮吞吐改进计划

- Status: `implementation-in-progress`
- Date: `2026-08-27`
- Baseline host: `pi05-cpu`
- Predecessor: commits `8ecc80b`, `75ae295`, `0f261db`, `1495f89`
- Scope: 回顾第一轮代码，收紧调度语义，建立可解释的性能观测，再优化单 Episode 转换成本
- Non-goal: 修改 gripper、selection、fps、分辨率、CRF 或其他训练语义

## 已知事实

1. BOS 只读、PFS 落盘的整条 staging 路径在 16 readers 时实测 `1.216 GB/s`；24 readers 退化到约 `0.199 GB/s`。这是当前 BOS→PFS 路径实测值，不是 BOS 服务的理论上限。
2. 96 个最小 Episode、约 `0.952 TB / 211,766 frames` 的端到端基线：64 conversion workers 用时 `1899.5 s`，80 workers 用时 `2078.2 s`，后者慢 `8.6%`。
3. 64/80 两档无新增 CPU throttling、OOM 或产物数量差异。增加 worker 并没有增加有效 conversion 并发。
4. 基线端到端平均消费源数据约 `0.50 GB/s`，低于 staging 的 `1.216 GB/s`。因此继续扩大 BOS 预取量不是当前提速主线。
5. 第一轮样本特意选了最小的 96 个 Episode，可用于比较 64/80，但不足以证明 64 是全局最优，也不应直接用 Episodes/hour 预测全量时间。

## 上一轮 commit 处置

| Commit | 结论 | 处置 |
| --- | --- | --- |
| `8ecc80b` bounded prefetch | 架构方向正确，但混入了主机/任务策略和未完整的水位语义 | 保留解耦、原子性和 resume；重构容量与观测部分 |
| `75ae295` benchmark tool | 保留了实验证据，但脚本是一次性 campaign runner | 用通用 benchmark harness 替换，再删除一次性逻辑 |
| `0f261db` PTY confirm | 仅用于绕过交互式 ENTER，不应成为正式执行依赖 | 删除 PTY 注入，benchmark 改用已冻结 manifest 的内部入口 |
| `1495f89` results/docs | 实测结果有价值，但“正式值冻结”表述过强 | 保留数据；改为 `pi05-cpu` 当前 baseline，不当作跨设备上限 |

## 继续保留的逻辑

- BOS source 只读，selection manifest 在执行前冻结。
- staging 使用独立 `ThreadPoolExecutor`，conversion 使用 `ProcessPoolExecutor(spawn)`。
- staging 和 conversion 可重叠，一方占满不会消耗另一方的 worker slot。
- staging 和 Fragment 使用隐藏临时目录、校验后原子提交。
- 成功/Excluded Episode 精确释放 staging；失败现场不为了继续调度而静默删除。
- run 内冻结调度参数，resume 拒绝无声更换语义。
- 写路径必须位于配置的 PFS containment root 下。
- `stage_workers=16` 和 `conversion_workers=64` 作为 `pi05-cpu` 下一轮基线，直到有代表性新数据否定它们。

## 需要删除或替换的逻辑

### 1. 删除通用库中的主机/任务硬编码

当前 `MAX_STAGE_WORKERS=32`、`MAX_CONVERSION_WORKERS=112`、`MAX_PREFETCH_BYTES=2 TB` 和 `MAX_PREFETCH_EPISODES=128` 被写在通用 config parser 中。它们来自本次 `pi05-cpu/fold_cloth` 策略，不是数据格式或转换算法的通用限制。

替换方案：

- 通用库只校验正整数、水位关系和路径 containment。
- `2 TB`、`16/64` 和任务 Episode 上限放入 deployment profile 或显式 resource policy。
- 若需防止操作员误配，由 profile lint 或 host policy 拒绝，不由通用 schema 假定全球上限。

### 2. 替换 `prefetch_target_bytes` 的假软水位

当前实现在 `reserved_bytes >= prefetch_target_bytes` 时立即停止新 staging，没有 low/high hysteresis。失败数据被保留后，还可能在未达 `prefetch_max_bytes` 时将调度终止。

替换为两类独立概念：

- `ready_low_bytes / ready_high_bytes`：只管理可供 conversion 消费的 staging 缓冲，低于 low 后补到 high。
- `temporary_hard_max_bytes`：统计本 run staging、隐藏 partial、保留失败现场和 Fragment 的临时占用，任何时候都不可越过。

2 TB 仍作为本任务的部署硬上限保留，但不再把 1.5 TB 当作日常必填充目标。第二轮从 `128/256 GB` low/high 候选开始，通过 conversion 空转时间决定是否扩大。

### 3. 删除无代表性的 benchmark 假设

当前脚本硬编码了 PFS root、日期型 run-id、dataset/repo/task/recipe，并只选最小 Episode。这些逻辑不进入下一版 harness。

新 harness 必须：

- 全部输入通过 CLI/profile 显式传入，run-id 自动唯一且可重入检查。
- 从冻结 manifest 按 source bytes/frame count/station/date 分层取样，不再只选最小项。
- 不向生产 CLI 伪造 PTY ENTER；使用已冻结 manifest 的 benchmark 内部入口。
- 同时输出 source GB/s、frames/s、wall time、phase time、p50/p95 Episode time、CPU/RSS/I/O pressure 和错误数。
- cleanup 只能删除本 benchmark run 的隔离目录，metrics 与配置永久保留。

### 4. 暂不删除 legacy shared-pool

schema v1/run schema v2 的 `_run_legacy` 仍是旧 run resume 兼容边界，现在删除会破坏可恢复性。处置方式：

1. 新 run 默认使用新 schema，不再新增 v1 profile。
2. 保留 v1 读取和 resume，并加 deprecated 提示。
3. 确认没有未完成 v1 run，并经过一个完整发布周期后，再单独提交删除。

## 第二轮开发

### P0：先修正调度和 resume 语义

1. 引入明确的 capacity ledger，区分 `active_stage`、`ready_stage`、`active_conversion`、`retained_failure`、`fragment` 和 hidden partial 字节。
2. 硬上限使用“已占用 + 待提交预留”判断；同时增加 PFS 最小剩余空间停止线。
3. 失败保留数据只在触发 hard max 时阻止新 staging，不得因 ready high watermark 导致假死。
4. resume 遇到无效或 identity 不匹配的 committed staging 时，移入 run 内 quarantine 保留证据，再从冻结 source identity 重建；不得反复重入同一失败目录。
5. 容量账本和作业状态的更新保持单写者，并通过故障注入验证 crash/retry。

### P1：补齐性能观测

当前 progress 只有队列数和预留字节，无法说明 conversion 慢在哪一段。下一版需要持久化 `metrics.jsonl`：

- stage queue wait、copy wall time、bytes/s、短窗口和累计 GB/s。
- conversion queue wait 以及 load/clean/classify/select/export/video/validate 各 phase wall time。
- Episode 级 source bytes、frames、segments、p50/p95/max wall time。
- conversion workers 实际 active 数、frames/s、source GB/s 和长尾等待时间。
- cgroup CPU use/throttling、anon/file memory、I/O pressure、PFS 剩余空间。
- 基于已完成 bytes/frames 的 ETA，目标是运行 20% 后误差不超过 `±15%`。

### P1：建立有代表性的新基线

1. 从全量 manifest 选择小/中/大 Episode，覆盖 w3/w4 和不同日期，固定一份 stratified sample。
2. staging 基线仍使用 16 readers，conversion 并发补测 `48/56/64`。64 是 baseline，不再向 80/96 扩展。
3. 分开记录 staging、Episode conversion、Builder 和 final validation，不只看一个端到端 wall time。
4. 确认新水位下 conversion starvation 接近零；如 `128/256 GB` 已足够，不再扩大 staging。

### P2：单 Episode 转换 A/B

只在 phase metrics 证明瓶颈后执行，每次只改一项：

1. 优先检查 MCAP 重复扫描和中间数据重复物化，尝试复用索引/一次解码结果。
2. 根据 SVT 实际 CPU/线程数测试 encoder 内部线程上限，避免 64 个 Episode worker 内部过度订阅。
3. SVT preset 候选必须比较 wall time、视频字节、可解码性和固定帧抽检；未通过前保持 preset 8 / CRF 30。
4. TorchCodec 只在其平台 wheel 和视频解码契约可验证时测试；当前 warning 本身不构成安装理由。

## Schema 与兼容性

- 如果替换水位字段，新 streaming config 使用 schema v3，不在 v2 上重解释旧字段。
- 新 run manifest 升级 schema，完整冻结 resource policy 和容量语义。
- v1/v2 config 及旧 run 继续按原语义读取/resume，禁止在 resume 时自动迁移。
- 新 profile 不再设置 `prefetch_target_bytes=1.5 TB`；2 TB 仅出现在 `pi05-cpu` 部署策略中。

## 开发与 commit 顺序

1. `test: specify streaming capacity and invalid-stage recovery`
   - 先增加会在当前实现上失败的水位、hard max、失败保留、无效 staging resume 和 PFS free-space 测试。
2. `refactor: separate streaming buffer and capacity policies`
   - 只实现 capacity ledger、low/high 水位和 schema 兼容。
3. `feat: persist streaming phase metrics`
   - 独立增加 phase timing、资源采样和 ETA，不混入 encoder 调参。
4. `tools: generalize streaming benchmark harness`
   - 先用新 harness 读取旧 metrics 并复现 baseline，再删除硬编码和 PTY 逻辑。
5. `perf: optimize measured conversion bottleneck`
   - 每个 A/B 候选独立 commit，只保留通过性能与产物契约的项。
6. `docs: freeze pi05 streaming profile after round two`
   - 最后才更新正式 profile、ETA 和运维指令。

每个 commit 必须可单独回滚；不再将 schema、scheduler、benchmark、文档和 1000+ 行测试混入同一个功能 commit。

## 静态与实测验收

- 单元测试覆盖上述 P0 状态，全量测试无回归。
- 新 run 的任何临时占用不超 deployment hard max，PFS 低于保留空间时不再发起 staging。
- 中断/resume 不重复 committed conversion，无效 staging 不循环失败，失败现场可审计。
- 同一 stratified sample 的 committed/excluded/discarded/failed、Episode 数、frame 数、gripper contract 和视频可解码性与 baseline 一致。
- 新默认配置相对代表性 baseline 至少降低 `15%` wall time；如果未达到，保留 16/64 基线并不合并未证明的性能改动。
- 300+ Episode 正式全量前，先用新 metrics 完成一次代表性小样本，给出按 bytes/frames 加权的 ETA 和资源边界。

## 当前决策

- 不继续提高 stage/conversion worker 数。
- 不立即安装 TorchCodec，不立即改 SVT preset。
- 暂不用当前 schema v2 profile 开始新的 300+ Episode 正式全量。
- 先完成 P0 语义修正和 P1 观测，再决定哪个 conversion 优化进入正式 profile。
