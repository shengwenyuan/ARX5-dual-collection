# 采集入口显式 Task Description TODO

- Status: `implemented / local verified / live multi-Episode pending`
- Date: `2026-08-25`
- Scope: 普通采集与 DAgger 采集入口、Episode metadata
- Non-goal: 在离线清洗阶段推断、归一化或改写任务语义

## 问题

当前生产 Compose 固定加载 `config/task.eight-stream.json`，导致所有 Episode metadata 的 `task.description` 都是通用八路采集描述。真实任务名目前只能在离线转换时另行传入，例如 `folding the cloth`；这使 MCAP/metadata 失去训练 prompt 的原始来源事实。

## 冻结需求

录制入口必须将 task description 提升为与输出根同级的显式必填参数：

```text
ARX5_OUTPUT_ROOT=/absolute/date/task
ARX5_TASK_DESCRIPTION="folding the cloth"
```

对应 CLI 必须显式接收：

```text
arx5-collect run ... --output-root ... --task-description "folding the cloth"
arx5-collect dagger ... --output-root ... --task-description "folding the cloth"
```

- 参数必须非空，不提供时拒绝启动采集 Session。
- 字符串原样写入每条 Episode 的 `metadata.task.description`，不得 trim 后改写、大小写归一化、翻译或从目录名猜测。
- 普通采集与 DAgger 必须复用同一 `EpisodeRequest` 字段和 metadata writer，不允许分叉实现。
- 八路 Task 配置继续承担 Topic、频率和稳定 `task_id` 契约；静态配置中的通用 description 不再作为生产 Episode 的任务语义。
- 一个长生命周期 Session 内的 Episode 默认继承该 Session 启动参数；切换任务时重新启动采集入口。未来 UI 若支持 Session 内切换，也必须通过同一核心参数更新边界。
- 输出路径与 task description 独立校验；路径名不得反向覆盖 task description。

## 迁移与兼容

- 既有 metadata 保持原样，不回写历史 Episode。
- 在该 TODO 实现前，叠衣服流式转换统一由版本化 recipe 显式配置 `task = "folding the cloth"`。
- TODO 实现并完成新数据验收后，离线转换应优先读取每条 Episode metadata 的原始 task；同一 Episode/Session 一致性检查继续生效。
- 旧数据需要训练 prompt 时必须使用显式 legacy recipe，不允许静默猜测。

## 验收

1. 普通和 DAgger 入口缺少 task description 时均在启动硬件前失败。
2. 连续两条 Episode 的 metadata 均精确等于入口字符串。
3. task 中包含空格、大小写和非 ASCII 字符时能够逐字保留。
4. 输出根名称与 task description 不同时，两者各自保持原值。
5. 离线清洗与 LeRobot 转换可直接使用新 metadata task，不再需要任务语义覆盖。

## 实现结论

- 普通与 DAgger Session 都要求 `--task-description`；缺失或仅含空白时在启动硬件前失败。
- Compose 统一从 `ARX5_TASK_DESCRIPTION` 传入，构建镜像时无需设置该变量。
- `load_request()` 仅覆盖静态 Task 配置中的通用 description；`task_id` 与八路 stream 契约保持不变。
- 参数只做非空判断，原字符串不经 trim、归一化或路径推断，继续由共享 metadata writer 写入。
- 普通 CLI、DAgger builder 与 metadata 定向测试已通过；连续两条真机 Episode 验收待下一轮采集完成。
