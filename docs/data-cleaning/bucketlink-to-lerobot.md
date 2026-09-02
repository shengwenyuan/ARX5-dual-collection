# BucketLink to LeRobot 设计

- Status: `implemented-locally`
- Date: `2026-09-02`
- Reuses: `docs/data-cleaning/cloud-streaming-lerobot.md`
- Scope: 以百度 PFS BucketLink 替换旧 BOS 文件复制，同时复用现有并行转换、Fragment、Builder 和验证逻辑

## 决策

独立入口：

```bash
arx5-dataset bucketlink-to-lerobot \
  --config <bucketlink-conversion.toml> \
  --output /absolute/lerobot/path \
  --run-id <run-id>
```

该入口复用 `arx5-dataset build` 的转换流程，只增加 BucketLink 传输和校验边界。

转换核心不复制一份：Episode 清洗、MCAP reader、并行 conversion worker、Fragment、
Builder、LeRobot validation 和 snapshot 继续复用现有模块。新入口只替换 BOS staging 的实现和
调度边界。

## 总体流程

```text
一个 <task>/<date> BOS prefix
  -> adapters/bos/bucketlink.py 创建并监督一个 BucketLink 导入任务
  -> 任务报告通过完整性校验
  -> 本 batch 进入现有 direct conversion worker pool
  -> 其他 batch 可由独立入口继续传输
  -> 所有 Episode 进入终态
  -> Builder 组装并验证不可变 LeRobot snapshot
  -> committed 后清理本次 PFS landing 数据
```

每天每个任务各运行一个入口，batch 边界固定为 `<task>/<date>`。并行启动多个入口时，某个已完成
传输的 batch 可以转换，其他 batch 的 BucketLink 继续运行；绝不读取尚未完成的 BucketLink 目录。
多个 ready batch 通过 PFS 上的全局 conversion slot 依次进入多进程 worker pool，避免多个 worker
pool 同时争抢同一台 CPU 设备。

这不再由 CPU 进程从 BOS 挂载逐 Episode 复制 MCAP。转换阶段始终读取 PFS，
并使用现有 `materialization = "direct"` 语义。该值在新入口中是固定实现事实，不作为用户可调
参数；仍写入 run manifest 供审计和恢复。

当前 landing root 默认使用：

```text
/mnt/pfs/swy/tmp/<dataset>/
```

配置模型允许未来更换为其他受控的 PFS 临时根目录，不把当前挂载路径写成永久产品约束。

## BucketLink adapter 边界

`src/arx5_collection/adapters/bos/bucketlink.py` 的职责限定为：

1. 使用官方 `baiducloud-python-sdk-pfs==0.0.4` 初始化 `PfsClient`。
2. 创建 `CreateL2BucketLink` 导入任务并取得 `bucketLinkId`。
3. 将请求参数、`bucketLinkId`、`requestId` 和最新状态持久化到本次 run。
4. 使用 `DescribeL2BucketLink` 轮询初始化、运行、成功或失败状态。
5. 成功后取得并校验任务报告的 total/skipped/failed counts；失败、取消或异常时停止。
6. 恢复运行时优先查询已记录的任务，不重复创建 BucketLink。

AK/SK 不进入 TOML、日志、异常文本或 Git。完整的 `BCE_ACCESS_KEY_ID`、
`BCE_SECRET_ACCESS_KEY` 环境变量对优先；两者都未设置时读取 `bcecmd -c` 生成的
`~/.go-bcecli/credentials`。只设置一个环境变量时拒绝运行，避免跨账号混用。任务报告
默认从 `/mnt/bos/<bucket>/...` 取得，也可用 `ARX5_BOS_MOUNT_ROOT` 调整挂载根。

该 adapter 不负责 MCAP 解码、Episode 清洗、LeRobot 写入和最终 PFS 清理。这些由
`dataset_pipeline/execution/bucketlink.py` 和 application/coordinator 层编排。

## 安全并行边界

不能仅因为 PFS 目标中出现 `episode.mcap` 就启动读取。运行中的 BucketLink 可能仍在写该
文件；任务被取消时也可能留下部分文件。因此，转换只消费完整的 `ready batch`。

一个 batch 的 ready 条件必须全部满足：

1. 对应 BucketLink 状态为成功。
2. 任务报告存在且 `failedCount == 0`、`skippedCount == 0`、`totalCount > 0`。
3. API 返回的源和目标与本次冻结定义一致。
4. PFS landing subtree 存在，且候选 Episode 均具有可解析的 `metadata.json` 和完整
   `episode.mcap`。
5. 文件 identity 在 PFS discovery 与 worker 打开时保持一致。

当 batch A ready 后，conversion pool 可以开始处理 A；此时 batch B/C 的 BucketLink 可以
继续运行。任何仍在初始化或运行中的 batch 都不能进入 conversion queue。

## 批次划分

BucketLink 请求只接受单一 BOS prefix，不提供任意 Episode 清单和排除规则。因此 batch 必须
对应一个真实、不可变的 BOS 目录 prefix，例如：

```text
<task>/<date>/
<task>/<date>/<session>/
<task>/<date>/<shard>/
```

一个配置、一个命令、一个 run 只接受一个 `<task>/<date>` prefix。多个任务或多个日期分别启动
独立命令，从而形成传输与转换重叠。不得通过观察文件大小稳定、尝试读取半成品或失败后重试来
伪造 Episode 级 ready。

不采用每 Episode 一个 BucketLink 作为默认方案。该方式会引入大量任务预处理开销，并受单个
PFS 实例并发任务上限约束。未来若需要更细粒度重叠，应优先让上传布局增加 session/shard 目录，
再以 shard 作为 BucketLink batch。

BucketLink 会导入 prefix 下的全部对象。原有 `block` 和 Episode 质量选择在 PFS discovery 后
继续生效，但无法节省这些被排除目录的传输开销；需要节省时应在 BOS 端提供更窄的 prefix。

DAgger 的训练资格保持与原转换链一致：`dagger + success` 与位于 `dagger_fail/` 下的
`dagger + fail` 都进入 DAgger authority selector；后者只贡献 fault 前已经完整闭合的
expert correction。`block = ["fail", ...]` 只屏蔽目录名精确为 `fail` 的普通失败目录，
不会屏蔽 `dagger_fail`。来源目录与 metadata 不一致时仍拒绝转换。

## 状态与恢复

新入口维护两级状态：

```text
Transfer batch:
  planned -> creating -> transferring -> verifying -> ready
                                      -> failed/cancelled

Episode conversion:
  discovered -> converting -> committed/excluded/discarded/failed
```

- 同一个 run-id 冻结 BucketLink 请求、batch 与 Episode lineage。
- 云端 `bucketLinkName` 固定为 `arx5-<run-id>`，保证以字母开头；本地 run-id 不变。
- 进程重启后查询已有 `bucketLinkId`，ready batch 不重复传输，已提交 Fragment 不重复转换。
- transfer failed 时不启动该 batch 的转换，并保留 PFS 现场。
- conversion failed 时保留对应 landing 数据和 Fragment 诊断。
- 只有最终 LeRobot snapshot committed 后，才清理本次 run 管理的 PFS landing 目录。
- 不自动取消运行中的 BucketLink，也不自动删除云端任务记录。

传输阶段中断后使用同一配置恢复；若 conversion manifest 尚未生成，仍需重复提供原输出路径：

```bash
arx5-dataset bucketlink-to-lerobot \
  --config <bucketlink-conversion.toml> \
  --output /absolute/lerobot/path \
  --resume <run-id>
```

## 配置草案

```toml
schema_version = 1

[bucketlink]
endpoint = "pfs.bj.baidubce.com"
instance_id = "pfs-..."
bucket = "datainfra-demo"
bucket_prefix = "uniqlo/2026-09-01/"
pfs_path = "/swy/tmp/uniqlo-2026-09-01/2026-09-01"
mounted_path = "/mnt/pfs/swy/tmp/uniqlo-2026-09-01/2026-09-01"
throughput_limit_bytes = 1572864000
conflict_policy = 2
report_prefix = ".baidu_l2_bucketlink_dflow/arx5/"

[source]
root = "/mnt/pfs/swy/tmp/uniqlo-2026-09-01"
include_paths = ["2026-09-01"]
block = ["fail", "abort", "logs"]

[runtime]
pfs_root = "/mnt/pfs/swy"
streaming_root = "/mnt/pfs/swy/dataset/1011/arx5/uniqlo/streaming"
conversion_workers = 64
temporary_hard_max_bytes = 2000000000000
min_free_bytes = 5000000000000

[output]
lerobot_root = "/mnt/pfs/swy/dataset/1011/arx5/uniqlo/lerobot"
dataset_name = "uniqlo_2026-09-01"
repo_id = "local/uniqlo_2026-09-01"

[recipe]
name = "pi05-equal-eef-v3"
profile = "../specs/recipes/pi05-equal-eef-v3-svt-p8.toml"
task_source = "metadata.task.description"
```

`pfs_path` 是 PFS API 使用的文件系统内部绝对路径；`mounted_path` 是 CPU 节点看到的挂载路径。
两者映射在对齐阶段必须验证。新配置不再包含 `stage_workers`、staging 水位或 BOS copy 预取参数。

## 首轮验收

1. SDK adapter 使用 mock 覆盖创建、查询、失败、取消和恢复，不访问真实云资源。
2. 使用两个独立 mini run：batch A ready 后开始转换，同时 batch B 仍处于 transferring。
3. 验证运行中或报告失败的 batch 永远不会进入 conversion queue。
4. 中断并恢复，确认不重复创建 BucketLink、不重复转换 committed Fragment。
5. 对同一白名单比较旧 streaming 与新 BucketLink 入口的 Episode lineage、frame count、task、
   video 参数和 LeRobot schema。
6. mini 通过后执行一次正式全量；在此之前不处理旧入口和旧文件删除。
