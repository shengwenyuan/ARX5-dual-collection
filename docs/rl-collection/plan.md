# infer 采集：首版实施与验收计划

更新：2026-09-20 · v0.4。状态：collection 首版代码已实现，离线验收完成；ROS 环境验证、真机验收与完成 commit 锁定待完成。

工作仓库：`/Users/shengwenyuan/1011/ARX5-dual-collection`。上层计划：[expo-online-RL/meta_plan.md](../../../expo-online-RL/meta_plan.md)。

## 1. 首版目标与三方边界

保留 MCAP 和 LeRobot，优先复用既有组件。collection 的新增与实质修改功能代码目标为 **600–1,100 行**，约为原估算的一半；测试、配置/schema、文档和自动生成代码另计。通过删除非首版职责收窄范围，不通过压缩代码写法或省略既有停止保护凑行数。

| 组件 | 首版职责 | 不在本组件重复实现 |
| --- | --- | --- |
| collection | 记录每控制步序号、时间、实际下发动作，保存 episode 结果与必要配置；正常结束录制并本地提交 | 数据质量审计、逐请求全链路追踪、训练窗口 |
| exporter | 复用 MCAP 解析、完整性/传感器/时间戳检查与视频编码；新增动作对齐、连续性检查和终止标记 | 复杂修复、损坏片段抢救、EXPO chunk/n-step/mask 算法 |
| EXPO replay | 使用官方 action chunk、n-step、mask 和跨 episode 边界处理，执行训练采样 | 不要求 exporter 预先生成另一套训练窗口 |

**普通任务 fail 是有效 RL 数据；录制损坏是数据质量失败。**左踏板 fail 不成为 exporter 的过滤理由。首版关键数据缺失时隔离整条受影响的 episode，不修复、插值或拼接剩余片段。

本文“整轮隔离”以一次采集 episode 为单位，不与包含多个 episode 的训练 round/batch 混用。隔离保留原始文件与原因，表示不接纳进训练；不是删除原始数据，也不自动作废同批其他合格 episode。

## 2. 保持已确认的采集流程

1. `metadata.json.collection_type` 新增 **`infer`**，与 `demonstration`、`dagger` 并列。
2. 运行中右踏板记录 `success`，左踏板记录 `fail`；二者均正常结束落盘，左踏板不接管、不 abort、不表示硬件故障。
3. 本局落盘提交后等待新的右踏板；右踏板触发 GO_HOME，归位就绪后启动下一局推理采集。结束本局不自动 HOME，不自动启动下一局。
4. HOME 与场景布置不进入任务 episode。复用既有设备绑定、HOME、RTC、控制 gate、停止保护、Recorder 和 EpisodeStore。
5. 阶段 A 固定已有 RTC checkpoint，不接候选/edit/Q 或参数热更新。无需专家演示和专家纠正片段。
6. 交付保持 `episode.mcap + metadata.json`。首版使用已有 `--no-compress` 跳过结束后重写压缩，exporter 继续以已提交 MCAP 为输入。

```text
READY 等右踏板 → GO_HOME → Recorder start / RTC bootstrap → RUNNING
  → 右 success / 左 fail → 停止策略动作 → 正常关闭录制并提交
  → READY 等新的右踏板
```

释放后重新按下才算新的启动事件；HOME/启动/落盘中的输入不缓存为下一局开始。冲突输入、abort 与实际故障沿用明确的停止路径，不伪造 success/fail。

## 3. collection 的最小记录契约

复用已有相机图像和关节反馈话题。首版原则上只增加一类轻量的**实际命令记录**；episode 结果和时间写入 metadata，不强制再建完整的推理事件总线。

| 位置 | 必要信息 |
| --- | --- |
| MCAP 原有传感器流 | 图像、关节反馈及其既有源时间戳 |
| 命令记录 | episode 标识、从 0 开始的 step 序号、实际发送时刻、最终双臂 14D 动作 |
| 时间基准 | 命令与传感器可比较的时间戳；若同时使用 ROS/设备时钟和单调时钟，记录必要映射，不能让 exporter 猜测 |
| metadata | collection_type=infer、episode ID、开始/结束/人工标注时间、outcome、结束原因、已下发步数（复用既有计数）、录制正常结束/提交状态 |
| 必要配置 | checkpoint/策略标识、动作维序与单位、夹爪契约、归一化标识、控制频率、当前 RTC 配置及记录 schema 版本 |

约束：

- 动作记录来自现有发送边界的最终目标值，包含既有限幅/变换结果；**不能用 measured state 代替 action**。不再额外保存每一层中间动作。
- 发送调用正常完成后记录该步；发送异常沿用现有停止和错误记录，exporter 隔离受影响 episode。首版不建设左右臂分别 ACK 或部分发送事务追踪。
- 记录的是“已下发目标”，不是“硬件确认执行到位”；不引入新的硬件确认协议。
- 保留完整实际执行序列和有效等待，不在采集端裁剪或筛选成功片段。
- 结束时先停动作，保留必要的后继传感器采样机会，再按现有流程关 Recorder。collector 不现场判定末帧是否适合训练；必要后继观测缺失由 exporter 隔离整局。
- 不新增观测复制通道或逐请求图像引用追踪。exporter 按执行步时间对齐训练观测；不把这种对齐声称为复原了原始 VLA 请求输入。
- 关键动作记录缺失可由 step 连续性、结束时间及结果信息识别。只加支持接入所需的轻量元信息，不加 request/chunk/command 全链路日志。

### 3.1 metadata 类型与结果

```json
{
  "collection_type": "infer",
  "outcome": "fail",
  "extensions": {
    "infer": {
      "schema_version": 1,
      "termination_reason": "human_label",
      "label_source": "left_pedal"
    }
  }
}
```

这是字段增量示意；时间与必要配置使用既有字段或小范围扩展。保留 outcome 的 `success | fail | aborted` 枚举，infer 不要求 dagger 对象或专家片段。正常 fail 不写成录制错误；故障按独立结束原因/已有错误字段表达。新旧类型的 writer/schema/reader 兼容性仍需验证。

## 4. 最小实施与代码量目标

使用薄的 `infer/` 模式组装，入口 `arx5-collect infer ...`（已实现），宿主机入口 `scripts/arx5 infer`。共享功能优先复用；模式差异集中在组装、踏板和 metadata，避免复制 runtime/scheduler 或散布条件分支。

| 改动 | 主要位置 | 功能代码目标（新增 + 实质修改） |
| --- | --- | ---: |
| infer 入口与组装 | `production/cli.py`、新 infer application，复用现有 session/策略客户端 | 180–300 行 |
| 踏板与正常结果 | trigger 适配、既有 episode hooks；HOME 原样复用 | 80–150 行 |
| infer metadata | `collection_metadata.py`、metadata builder | 60–100 行 |
| 每步动作事实 | 发送边界轻量 hook、消息声明/发布、recorder additional_topics | 180–350 行 |
| 停止与落盘接线 | 既有 runtime/recording/store，小范围配置与结束信息接线 | 100–200 行 |
| **合计** | 不含测试、JSON/TOML schema/config、文档和自动生成代码 | **600–1,100 行** |

- 不新增 collection 专用 MCAP 回读审计器、传感器对齐器或 replay window 构造器。
- collection 只确认录制正常结束和本地提交成功，不输出“数据可训练”判定。既有 backend 附带的统计/校验可保持，不为 infer 再增加扫描、序列审计和图像核对层，也不为删掉旧检查做大重构。
- 保留既有停止保护、设备错误和 recorder 失败处理；新增要求不包含复杂自动恢复。
- 代码量是设计目标，不是正确性的替代标准。预计超出时先检查是否重复实现或混入延期项，并重新说明边界。

## 5. exporter 的首版接入与拒收原则

```text
MCAP + metadata（collection 已提交）
  → exporter 复用解析/传感器检查，做动作对齐与连续性检查
  → 合格 episode：终止/奖励原始信息 + LeRobot candidate
  → 关键缺失 episode：整局隔离 + 原因，保留原文件
  → EXPO adapter/replay：官方训练采样
```

- MCAP 完整性、缺帧、时间戳异常、动作/图像对齐由 exporter 为主，复用既有检查。collection 不重复建设这些功能。
- exporter 仅新增实际 command 解析、按控制步的因果对齐、连续性和终止标记。原有重采样/静止裁剪/尾段裁剪不适用时绕开，不复制整套管线。
- 关键缺失包括必要动作/结果丢失、无法得到合法前后观测或超出阈值的连续性破坏；不是把所有重复图像或偶发相机间隔都机械判为坏数据。
- 第一版隔离整条不合格 episode，不补帧、不插值动作、不抢救中间 interval、不跨断点拼接。
- discovery、质量检查和 recipe 均不能仅因 `outcome=fail` 或 fail 目录名拒收 infer episode。合格 success/fail 均进入可接纳集合；后续明确配置的数据选择与 minibatch 比例是另一层决策。
- 任务结果、数据质量结论分开保存；successful 但损坏的 episode 也须隔离。原始 metadata 不因导出结果原地改写。
- exporter 保留 episode 边界、连续步序、终止/截断原因、必要末帧与原始奖励信息。action chunk、n-step 回报、mask/padding 和跨 episode 采样由官方 EXPO replay 生成，exporter 不再实现一份。
- 上传对象和上层 PFS/完整 dataset 合并流程按主计划执行。本计划不增加 collector 到 exporter 的实时 ACK 系统。

## 6. 明确延期的内容

- request → chunk → command 全链路追踪、逐请求推理输入精确复原。
- 左右臂分别执行确认、发送事务追踪和复杂恢复。
- 被丢弃候选/推理响应的详细记录、候选分数与 edit/Q 诊断。
- collection 新增的完整性审计器、观测引用审计、全套运行时诊断报表。
- exporter 的坏片段抢救、插值修复、复杂 interval 切分和训练 window 构造。

阶段 B 若官方 EXPO 接入需要当前无法重建的训练字段，在明确算法输入契约后做最小增量；不以可能需要为由提前实现整套审计。既有 RTC 的调度、prefix 和停止逻辑不因日志延期而删除。

## 7. 实施、验收与 commit 锁定

**本仓库补齐功能 → 验收明确通过 → 提交完成版本 → 主仓库锁定 commit。**当前已完成 collection 代码与离线测试，按用户要求停在真机测试前；未运行机器人；按用户追加要求提前提交开发版本，最终完成版本与 gitlink 仍待真机验收。原有其他未提交内容保持不变。实现与验收入口见 [implementation.md](implementation.md)。

| 阶段 | 交付/验收 |
| --- | --- |
| C0：最小契约 | 冻结命令字段、时间基准、infer metadata 和 exporter 接口；确认官方 replay 所需边界信息 |
| C1：模式接线 | 右启动/HOME、左右结果、既有 RTC 与 MCAP 提交；假设备成功/失败流程和旧模式回归 |
| C2：事实记录 | 验证记录的 step/time/action 等于发送边界实参，metadata 结果正确，记录服务可正常结束；不开发独立运行审计系统 |
| C3：小样本交接 | 使用 exporter 既有 reader/检查验证成功和任务失败都可接入；坏样本整局隔离；对接官方 replay 的边界样例 |
| C4：实机 | 既有运行环境下验证连续多局、右 HOME/启动、左 fail/右 success、正常录制关闭与动作记录；保留简洁验收记录 |
| C5：完成版本 | 包含必要代码/schema/config/测试的提交可复现；通过后再由主仓库锁定 SHA |

验收职责也按三方划分：

- **collection：**类型、踏板/HOME、每步动作记录、正常关闭提交与旧停止保护；重点确认未复制 RTC 或新增审计子系统。
- **exporter：**MCAP 回读、对齐/连续性、终止标记、fail 可用与坏 episode 隔离。必要缺失的注入样例在这里验证。
- **EXPO replay：**通过小样本适配验证官方 chunk/n-step/mask 及 episode 边界行为；不在 collection/exporter 写同构实现。

collection 验收可以使用 exporter 小样本接入验证输出契约，不要求先完成云端上传训练。假设备测试不能代替实机；实机结果也不代表整个学习链路已经完成。

## 8. 当前状态与版本记录

| 项目 | 当前状态 |
| --- | --- |
| 计划 | v0.4，保持 v0.3 的三方边界 |
| C0 | collection 命令/metadata 契约已落盘；官方 replay 适配仍属后续 |
| C1 / C2 | 功能已实现；全量离线测试 394 passed、1 skipped（新增 28 项），源码改动约 548 行；ROS 消息编译和实际 MCAP 连通尚待 Linux 环境验证 |
| C3 | exporter 接入、数据质量隔离和 replay 联调未执行，本轮未修改这两个模块 |
| C4 | 未运行真机，等待用户配合；踏板释放/长按行为必须现场验收 |
| 开发 commit / 完成版本 | 开发代码已提前提交；最终验收完成版本、主仓库锁定待完成 |

- v0.1：无 MCAP 与实时 sink 方案，已撤销。
- v0.2：保留 MCAP，确认 infer、左 fail/右 success、落盘后右踏板 HOME。
- v0.3：collection 只记必要事实，exporter 判定可用性并隔离关键缺失 episode，官方 replay 负责训练采样；删除首版完整追踪审计要求，功能代码目标 600–1,100 行。

- v0.4：实现 collection infer 模式与离线验收；保留 MCAP、默认关闭结束后压缩，明确尚未完成 ROS/真机与 exporter/replay 验收。
