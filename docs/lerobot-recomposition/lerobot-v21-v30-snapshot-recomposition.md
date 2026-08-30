# LeRobot Snapshot 重构与多源拼配设计

- Status: `implemented / Mac-validated / cloud deployment pending`
- Date: `2026-08-30`
- Scope: 已提交 LeRobot snapshot 的拆分、选择、拼配与新 snapshot 原子发布
- Immediate backend: `lerobot-v2.1`
- Future backend in the same development round: `lerobot-v3.0`
- Parent: `plans/incremental-lerobot-build-queue.md`
- Non-goals: 重新读取 MCAP、修改训练样本数值、隐式重标 task、训练期 sampler weighting、Hub 发布

## 与现有 Streaming 链路的隔离边界

本功能只新增独立 composition package、配置 schema、CLI handler 和 backend，不修改现有 `stream-to-lerobot` 的 discovery、alignment、run manifest、prefetch coordinator、Fragment、Builder、recipe 或 validator 调用路径。已有 MCAP → LeRobot 命令、配置及 resume 语义必须保持逐项不变。

首版 v2.1 composer 可以复用通用 artifacts/atomic helper，但不抽取或改写正在生产使用的 streaming Builder 私有实现。待 composer 经真实数据集验证后，若确有必要共享底层函数，必须另立纯重构提交，并以 streaming Builder 产物等价回归作为合入门槛。

## 目标

新增独立入口 `compose-lerobot`，从一个或多个不可变 LeRobot snapshot 选择既有 Episode，确定性构建新的 standalone snapshot：

```text
committed LeRobot snapshots
  -> 读取格式、训练契约与 provenance
  -> 解析并冻结精确 Episode allowlist
  -> 检查重复来源与跨源契约
  -> 按 recipe 顺序重编号 Episode / frame / task
  -> backend materialization
  -> LeRobot + OpenPI + provenance 验证
  -> 原子发布新的 committed snapshot
```

近期必须可直接用于现有 π0.5/OpenPI 链路的 v2.1 数据集构建。v3.0 backend 在同一轮完成代码与 fixture 验收，但不得改变或替换 v2.1 backend；两个 backend 使用独立格式适配层和独立 pinned runtime。

## 术语和“无损”边界

- **source snapshot**：只读输入；必须已经完整提交，重构过程中不修改。
- **logical Episode**：训练侧可见的一个 LeRobot episode。它通过项目 `source_manifest.jsonl` 映射回 source Episode、source Session 和 segment。
- **composition recipe**：输入 snapshot、精确选择、稳定顺序、输出 backend 和输出身份的声明。
- **materialization**：把逻辑选择发布成一个可独立加载的新 LeRobot snapshot，而不是只返回运行时视图。
- **训练语义无损**：state、action、timestamp、frame_index、task、Episode 边界、视频编码 packet 表达的帧序列不改变。
- **文件字节相同**：只对直接 hardlink/copy 的 MP4 承诺。Parquet 因全局索引重编号必然重写；MP4 remux 后容器字节也可能改变。

首版禁止视频解码后重新编码。若一个操作无法通过 hardlink、copy 或 packet-level remux 完成，必须拒绝并说明原因，不能用“无损”掩盖有损视频重编码。

## 权威版本

### LeRobot v2.1

当前项目与 OpenPI 已锁定：

```text
LeRobot commit: 0cf864870cf29f4738d3ade893e6fd13fbd7cdb5
dataset codebase_version: v2.1
project backend: lerobot-v2.1
```

v2.1 是 Episode-based 布局：每个 Episode 独立 Parquet、每个相机每个 Episode 独立 MP4，元数据为 JSONL。现有 `streaming_conversion.builder` 已验证 Parquet 重编号、task 合并、视频 hardlink/copy、原子发布和 validator；本轮只将它作为行为参考，不抽取或改写其生产实现。

### LeRobot v3.0

首轮目标 pin 为 LeRobot package `v0.6.1` / commit `7e241bd630a3719a56157a497ce5d08f244784f1`，其 dataset `CODEBASE_VERSION` 仍为 `v3.0`。正式编码前再做一次依赖锁与 Linux CPU smoke；通过后固定 commit，不跟随 main。

官方 v3.0 改为 file-based 布局：多个 Episode 共享 Parquet/MP4 shard，Episode 边界由 metadata 中的 file index、frame/byte/timestamp offset 表达。官方已经提供 `split_dataset`、`merge_datasets` 和 v2.1→v3.0 converter，因此 v3 backend 应调用并约束官方实现，不自行复制一套 v3 文件格式。

参考：

- [LeRobotDataset v3.0 格式设计](https://github.com/huggingface/lerobot/blob/main/docs/source/lerobot-dataset-v3.mdx)
- [官方 Dataset Tools：split / merge](https://github.com/huggingface/lerobot/blob/main/docs/source/using_dataset_tools.mdx)
- [官方 v2.1→v3.0 converter](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/scripts/convert_dataset_v21_to_v30.py)
- [官方 v3 aggregate backend](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/datasets/aggregate.py)

## 冻结原则

1. source snapshot 永远只读；输出永远是新目录，已存在即拒绝。
2. 只消费 `snapshot.json.status == "committed"` 且能通过对应 backend validator 的输入。
3. 首版只接受项目生成、带 `reports/source_manifest.jsonl` 的 snapshot；不根据目录名猜 provenance。
4. 选择最小身份是 `segment_id`，并同时冻结 `source_episode_id`、`source_session_id` 和原 `lerobot_episode_index`。
5. 同一 `segment_id` 不得在一个 composition 中出现两次；同一 source Episode 的多个合法 segment 可以全部保留，但不能跨 split 泄漏。
6. 输入顺序与每个输入内 allowlist 顺序共同决定输出 Episode 顺序；不依赖文件系统遍历顺序。
7. task 原样保留并按首次出现顺序建立全局 task index。首版不提供 task relabel。
8. 不通过复制 Episode、复制 frame 或重复视频实现权重。比例控制必须先解析为精确、无重复的 allowlist。
9. 所有 backend 共享相同的逻辑计划、兼容性检查、provenance 和结果报告；只有物理布局读写不同。
10. 对齐阶段只读，不创建输出；用户按 Enter 后才冻结 manifest 和开始 materialization。

## 对外入口

```bash
arx5-dataset compose-lerobot \
  --config config/composition.<name>.toml
```

入口流程固定为：

1. 加载配置和所有 source snapshot。
2. 输出 source format、repo_id、合同摘要、命中 Episode/segment、frames、tasks、视频数量与预计操作类型。
3. 明确标记每路视频为 `hardlink`、`copy`、`packet-remux` 或 `rejected`。
4. 等待交互式 Enter。
5. 在输出同级隐藏目录构建，验证通过后一次 rename 发布。

不增加跳过确认的生产参数。测试通过内部 application 入口注入已冻结 plan，不伪造 PTY。

## Composition TOML

首版 schema：

```toml
schema_version = 1

[output]
backend = "lerobot-v2.1" # 或 lerobot-v3.0
repo_id = "local/fold_cloth_2026-08-26_2026-08-27"
path = "/absolute/pfs/path/local/fold_cloth_2026-08-26_2026-08-27"

[[sources]]
name = "fold_2526"
path = "/absolute/path/local/fold_cloth_2026-08-25_2026-08-26"
selection_manifest = "composition/fold_2026-08-26.jsonl"

[[sources]]
name = "fold_27"
path = "/absolute/path/local/fold_cloth_2026-08-27"
select_all = true
```

当 output 或任一 source 为 v3.0 时，必须额外显式指定独立 runtime；v2.1-only 配置不得要求该字段：

```toml
[v3_runtime]
python = "/absolute/path/to/lerobot-v3-venv/bin/python"
```

该环境固定安装项目的 `dataset-v3` extra，即 LeRobot `v0.6.1` / commit
`7e241bd630a3719a56157a497ce5d08f244784f1`。CLI 不查找 PATH 中的任意 Python，
也不在运行时安装或升级依赖。

每个 source 必须且只能使用一种选择方式：

- `select_all = true`；或
- `selection_manifest = <path>`，文件按目标顺序冻结精确 `segment_id`。

不在 materializer 内实现日期 glob、模糊 task 匹配或随机比例。单独的只读 planning helper 可以从 source manifest 依日期、Session、task 或确定性 seed 生成候选 allowlist；最终 recipe 始终引用可审阅的精确 manifest。

`selection_manifest` 每行至少包含：

```json
{
  "segment_id": "...",
  "source_episode_id": "...",
  "source_session_id": "...",
  "expected_lerobot_episode_index": 123
}
```

四个字段必须与 source snapshot provenance 完全一致，否则拒绝，防止相同索引在新版本中静默指向其他 Episode。

## 共享领域模型

格式 reader 先把 v2.1/v3.0 都解析为只读逻辑描述：

```text
SnapshotDescriptor
  identity
    root / repo_id / backend / format commit
    metadata fingerprint
  contract
    features / fps / robot_type
    state-action order and version
    camera keys / RGB / resolution
    gripper contract and normalization
    sampling/filter/OpenPI contract
    video codec / pixel format / fps
  episodes[]
    local episode index / length / tasks
    source Episode / Session / segment / split group
    physical parquet reference
    physical video shard + offset reference
```

Planner 只操作 `SnapshotDescriptor`，输出 backend-neutral `CompositionPlan`。Backend 只能消费已经冻结且验证过的 plan，不能重新选择 Episode。

## 跨源兼容性 Gate

以下任一项不一致都拒绝，不做隐式转换：

- feature keys、dtype、shape、names；
- fps；
- robot_type；
- state/action 维度、顺序、单位和 proxy version；
- 相机 key、RGB 语义与分辨率；
- gripper contract、raw 边界和 normalization；
- sampling、filter、action horizon 与 OpenPI contract；
- video codec、pixel format、分辨率和 fps；
- 同一 source Episode/Session 内的 task 一致性；
- 重复 segment 或 lineage 冲突。

不同 task prompt 可以共存于一个多任务 snapshot，只要上述训练字段契约一致。task 不因为父任务关系而改写：fold_cloth 仍保持 `folding the cloth`，uniqlo 的两个原生 prompt 也分别保留。

## v2.1 Backend

### 支持范围

- 输入：一个或多个项目 committed v2.1 snapshot。
- 选择：任意 logical Episode/segment allowlist。
- 输出：standalone committed v2.1 snapshot。
- 视频：同文件系统 hardlink；hardlink 不可用时逐字节 copy；不解码、不 remux、不重编码。

### Materialization

1. 按 plan 顺序读取每个选中 Episode 的独立 Parquet。
2. 只重写全局 `episode_index`、`index` 和合并后的 `task_index`；`frame_index` 保持 Episode 内从零连续。
3. 复制其他列及其 Arrow 类型，不将数据转成 Python row 后重建。
4. 重映射 `episodes.jsonl`、`episodes_stats.jsonl` 和 `tasks.jsonl`。
5. 按新全局 Episode 路径 hardlink/copy 每路 MP4。
6. 生成新的 info totals、train split、source manifest、composition report、validation 和 snapshot marker。

现有 streaming Builder 不能原样作为 composer：它耦合 `RunManifest + FragmentDescriptor`。首版 composer 独立实现纯 v2.1 snapshot reader/reindex/writer，避免伪造 streaming run 或 Fragment，也避免改动当前生产链路。

## v3.0 Backend

### 独立运行时

v2.1 pinned commit 与 v3 package 不能安装进同一 Python 环境。主进程不动态替换 `lerobot`：

```text
main project / pinned v2.1 runtime
  -> freeze CompositionPlan JSON
  -> invoke pinned v3 worker subprocess
  -> v3 worker validates plan and source metadata again
  -> write result JSON
```

建议新增独立、锁定的 v3 runtime project，例如 `tools/lerobot_v3_runtime/`，由部署脚本建立专用 venv。主 CLI 只接受该 worker 的显式路径或已验证的 deployment profile；不在运行时联网安装依赖。

### v3 source 与输出矩阵

| Input | Selection | v3 output | 首版策略 |
| --- | --- | --- | --- |
| v2.1 | 任意 Episode | 支持 | 先用 v2.1 backend 生成选中临时 snapshot，再在隔离目录迁移为 v3 |
| v3.0 | 整份 snapshot | 支持 | 官方 `merge_datasets(..., concatenate_videos=False)` |
| v3.0 | 覆盖完整视频 shard | 支持 | copy shard，重写 Parquet/metadata index |
| v3.0 | 切开共享视频 shard | 拒绝 | 官方 split 可能重编码；首版不放宽无损边界 |
| v2.1 + v3.0 | 上述合法选择 | 支持 | v2.1 临时迁移后统一调用官方 v3 merge |

官方 v2.1→v3.0 CLI 对本地数据采用原位目录换名并保留 `_old`，不直接用于生产 source。v3 worker 应在 output 同级的隔离临时目录调用其纯转换步骤，源目录保持只读；完成 validator 后再原子发布。

官方 v3 `merge_datasets` 在 `concatenate_videos=True` 时使用 packet-level remux，不解码；为了更容易审计和避免跨源文件改写，首版固定 `concatenate_videos=False`。Parquet 和 metadata 仍会因全局索引与 shard mapping 发生重写。

## Provenance 与输出报告

每个输出都包含项目 sidecar，不依赖 LeRobot 格式自行表达 ARX lineage：

```text
snapshot.json
reports/
  composition.json
  source_manifest.jsonl
  validation.json
  rejected.jsonl
meta/ data/ videos/          # LeRobot 自身格式
```

`composition.json` 至少记录：

- composition schema、backend、项目 commit、LeRobot runtime commit；
- 输入 root、repo_id、format、metadata fingerprint；
- 每个 source 的 selection manifest 和选中计数；
- 输入顺序、输出 episode/frame/task/video 数；
- hardlink/copy/remux 计数与总字节；
- 兼容性 contract 摘要；
- 输出验证结果。

`source_manifest.jsonl` 保留原字段并追加：

- `composition_source`；
- `source_repo_id`；
- `source_lerobot_episode_index`；
- 新的 `lerobot_episode_index`。

不得丢失 `segment_id`、`source_episode_id`、`source_session_id`、`split_group`、collection/training class 或 authority lineage。

## 原子性与失败恢复

1. source 只读，output 不存在。
2. 在 output 同级创建唯一隐藏 staging 目录。
3. plan、每个已完成 Episode/shard 和操作类型写入 journal。
4. 任一异常保留 staging 与报告，不生成 committed `snapshot.json`。
5. 所有格式验证、计数闭合和抽样语义检查通过后，最后写 `snapshot.json`，再 rename 发布。
6. 首版失败后不自动猜测 resume；显式 resume 只能继续同一个冻结 plan fingerprint。

## 验证

### 所有 backend

- 输入/输出 segment 集合完全相等且无重复；
- Episode、frame、task、video 与 provenance 计数闭合；
- 每个 Episode 的长度、task、source identity 和 split group 一致；
- state/action/timestamp/frame_index 抽样逐值相等；
- 每路视频抽检首/中/末帧的 PTS、分辨率、颜色和解码结果；
- source 文件未发生 mtime/size 变化；
- OpenPI loader 可按 action horizon 50 读取头、中、尾样本。

### v2.1

- 所有 MP4 与 source inode 相同或 SHA-256 相同；
- Parquet 除三个全局索引字段外逐列逐值相同；
- pinned v2.1 `LeRobotDataset` 与现有 `validate-pi05` 通过。

### v3.0

- pinned v3.0 `LeRobotDataset` 加载通过；
- metadata 中 data/video file mapping、frame offsets、timestamps 和 episode boundary 闭合；
- whole-shard copy 的 MP4 SHA-256 相同；packet-remux 路径验证编码 packet/frame 语义，不宣称容器字节相同；
- v3→v3 full merge 与 v2.1→v3 migration 各有 fixture。

## 测试层次

1. 纯 planner：选择、顺序、重复、task、contract、plan fingerprint。
2. v2.1 fixture：拆分、两源拼配、多 task、一个 source Episode 多 segment、hardlink fallback。
3. v3 fixture：v2.1→v3、v3 full merge、完整 shard 选择、切 shard 拒绝。
4. corruption：缺 metadata、错 totals、断裂 lineage、错视频、已存在 output、原子失败。
5. cross-backend semantic comparison：同一小数据分别输出 v2.1/v3.0，比较 logical samples 与 task/source lineage。
6. 真实 mini：从现有 25+26 snapshot 选少量 26 日 segment，再拼入 Q1 的 27 日 snapshot。

## 开发顺序

1. 冻结 composition schema、domain model、alignment 和 provenance validator。
2. 独立实现纯 v2.1 snapshot backend，不改动 streaming Builder；只复用稳定的 artifacts/atomic helper。
3. 实现 v2.1 `compose-lerobot`、fixture、OpenPI smoke；这是近期数据构建的发布门槛。
4. 建立 pinned v3 独立 runtime 与 JSON worker protocol。
5. 实现 v2.1→v3 migration、v3 full merge、whole-shard selection 和拒绝边界。
6. 完成 cross-backend fixture 与 Linux CPU mini。
7. 只先部署 v2.1 backend 构建当前配方；v3 backend 在 runtime 部署验收后单独启用。

## 当前数据集配方

v2.1 backend 首批构建：

1. 从既有 25+26 snapshot 精确 materialize 独立 `fold_cloth_2026-08-26`。
2. Q1 committed 后构建 `fold_hq_2026-08-26_2026-08-27`。
3. 构建 `fold_all_2026-08-25_2026-08-26_2026-08-27`；25 日优先复用 contract 一致的现有资产，否则从 25+26 snapshot 拆出。
4. Q3 + Q4 构建 `uniqlo_native_2026-08-27_2026-08-28`，保留两个原生 task prompt。
5. 根据精确 allowlist 构建 `uniqlo_fold_aux_v1`，fold 辅助 Episode 不重标为完整 uniqlo task。

## 已冻结决策

1. v3 runtime 固定 LeRobot `v0.6.1` / commit `7e241bd630a3719a56157a497ce5d08f244784f1`，通过独立 venv/worker 部署。
2. v3 首版拒绝会切开共享 MP4 shard 的任意 Episode 子集；不提供隐式视频重编码。
3. 首版只接受带项目 `snapshot.json + reports/source_manifest.jsonl` 的 ARX snapshot，不支持缺 provenance 的第三方 LeRobot。
4. 首轮实现并测试混合 v2.1+v3.0 输入到 v3.0，但当前生产 recipe 暂不使用。
5. 用户入口统一为 `compose-lerobot`，通过 `output.backend` 选择 backend。
6. 本机 Mac 完成实现与 focused tests 后才能同步 `pi05-cpu`；开发阶段不得触碰云端运行代码。

## 本轮实现与本机验收

- 新入口：`arx5-dataset compose-lerobot --config <composition.toml>`。
- 新实现位于 `arx5_collection.lerobot_recomposition`，现有
  `streaming_conversion` package 未修改。
- v2.1：真实 Arrow schema 保留与索引重写、精确 allowlist、task/provenance 重映射、
  MP4 hardlink/copy、原子发布和失败 staging 保留均通过 fixture。
- v3.0：独立 worker、固定 runtime、v2.1 临时副本迁移、官方 aggregate、完整 shard gate、
  shard component 顺序约束和禁止重编码均已实现。
- 本机完整项目测试：`454 passed, 1 skipped`。
- pinned LeRobot `0.6.1` 独立环境已通过 API smoke，并以两个无视频 v3 source 完成实际
  aggregate/load smoke。Mac 的 TorchCodec 因本机 FFmpeg 动态库不可载而回退 PyAV；
  这不影响本次 metadata/data smoke。

在启用云端 v3 production backend 前，仍需在 Linux CPU 环境对真实视频完成一次
v2.1→v3 migration 和 v3 whole-shard merge mini。近期生产构建只启用已在现有运行时验证的
v2.1 backend。
