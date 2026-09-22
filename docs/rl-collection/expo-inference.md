# EXPO 两阶段 infer 接入

这是阶段 A 纯 RTC 采集之外的可选后端，算法由 expo-online-RL 提供。原有 DAgger 和 RTC 默认行为保留。

在私有原生 inference.toml 中增加 `[expo]`：`bundle`、`bundle_id`、`source_root`、`upstream_root`，并使用 `model_variant="arx5_joint_rtc"`。准备入口会生成只读挂载及 effective TOML；模型服务核对完整发行包与实际合同后预热。主仓库的配置示例包含 overlay。

本轮合同为30Hz/H30/C8/d4，`rtc.minimum_execution_horizon=4`对应effective的`prefetch_after_steps=4`。第一局d0也经过edit/Q；续局第4步prepare，到第8步取更新观测finish。只发经过Q/edit选择的C步，不提前拼接、不发base尾部。迟到、epoch失效、prefix变化沿用关闭gate和人工接管保护。

`[recording] enabled=false`使用相同模型client、动作调度、传感器健康监测和踏板生命周期，不建立EpisodeStore/MCAP或命令记录publisher。默认true；false模式的结果不是replay数据。safe_infer集成暂不展开。

EXPO policy ID和会话推理seed进入现有静态episode配置，不新增高频MCAP话题。既有命令记录开销仍需开关对照测试，不能由静态测试宣称零延迟差异。

本地静态测试不代表5080时序或真机验收通过。下载和服务预热不自动激活策略；人在设备空闲、完成无动作验收后，显式运行现有infer入口并操作踏板。本轮开发没有启动机器人。
