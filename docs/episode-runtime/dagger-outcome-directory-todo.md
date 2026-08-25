# DAgger 异常 Outcome 独立目录 TODO

- Status: `todo`
- Date: `2026-08-25`
- Scope: 采集侧 EpisodeStore 的异常目录路由，以及离线后处理的来源约束

## 目标

DAgger 异常 Episode 必须与普通采集异常 Episode 在目录层显式区分，便于白名单选择、审计和保留人工 correction：

```text
普通采集 fail/aborted       -> 普通异常，只审计
DAgger fail/aborted         -> DAgger 异常来源，保留 authority 审计能力
```

当前 DAgger `fail` 已通过配置落入 `dagger_fail/<episode_id>`；DAgger `aborted` 仍复用普通 `aborted/`。后续应把两类 DAgger 异常都改为显式、稳定且互不混淆的目录标记。`dagger_fail/` 名称保持；DAgger aborted 的最终目录名在实现前单独对齐。

## 实现边界

- 不在 DAgger application 中增加散落的路径判断；扩展 `EpisodeStore` 的 outcome 目录映射，由普通 Session 与 DAgger Session 分别注入 profile。
- 普通采集现有 `fail/`、`aborted/` 语义和路径不变。
- metadata 的 `collection_type` 与 `outcome` 始终是事实来源；目录名只用于发现边界、来源约束和人工审计。
- `dagger_fail/` 必须验证为 `collection_type=dagger + outcome=fail`，不匹配时拒绝转换。
- `dagger_fail` 可提取 `FAULT_HOLD` 前已经完整闭合的 expert correction；未闭合 correction 和 fault 后区间排除。
- DAgger aborted 是否允许保留中止前已闭合 correction，必须在其终止语义和真实样本验收后另行冻结；本 TODO 不预设训练资格。

## 验收

- 普通与 DAgger 的 success/fail/aborted 路由测试完整，互不改变现有 outcome metadata。
- DAgger 异常目录中的 metadata/path 不一致会被 discovery 或后处理拒绝。
- 流式转换可以用白名单单独选择 DAgger 异常来源，同时保持普通异常默认排除。
