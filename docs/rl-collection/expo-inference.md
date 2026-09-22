# EXPO 两阶段 infer 接入

EXPO首窗在动作gate关闭时等待完整prepare/finish，使用独立的
`[gateway] expo_bootstrap_timeout_s = 2.0`（秒，必须正且有限，并不超过transport timeout）。
它不改变续窗d/C、prefetch或快阶段一帧截止；普通RTC仍使用原policy_wait_timeout_s。
关闭录制时只结束实时流监测，不审计MCAP；退出清理异常不能覆盖原始推理异常。

这是阶段 A 纯 RTC 采集之外的可选后端，算法由 expo-online-RL 提供。原有 DAgger 和 RTC 默认行为保留。

在私有原生 inference.toml 中增加 `[expo]`：`bundle`、`bundle_id`、`source_root`、`upstream_root`，并使用 `model_variant="arx5_joint_rtc"`。准备入口会生成只读挂载及 effective TOML；模型服务核对完整发行包与实际合同后预热。主仓库的配置示例包含 overlay。

本轮合同为30Hz/H30/C8/d4，`rtc.minimum_execution_horizon=4`对应effective的`prefetch_after_steps=4`。第一局d0也经过edit/Q；续局第4步prepare，到第8步取更新观测finish。只发经过Q/edit选择的C步，不提前拼接、不发base尾部。迟到、epoch失效、prefix变化沿用关闭gate和人工接管保护。

`[recording] enabled=false`使用相同模型client、动作调度、传感器健康监测和踏板生命周期，不建立EpisodeStore/MCAP或命令记录publisher。默认true；false模式的结果不是replay数据。safe_infer集成暂不展开。

EXPO policy ID和会话推理seed进入现有静态episode配置，不新增高频MCAP话题。既有命令记录开销仍需开关对照测试，不能由静态测试宣称零延迟差异。

本地静态测试不代表5080时序或真机验收通过。下载和服务预热不自动激活策略；人在设备空闲、完成无动作验收后，显式运行现有infer入口并操作踏板。本轮开发没有启动机器人。

### 窗口连续性拒绝诊断

RTC / EXPO 接受新窗口时，同时检查实测关节状态到首个目标、上一条成功发布目标到首个目标，以及窗口内部逐步差值。均沿用配置的关节 step 上限，不放宽阈值、不进行动作平滑或截断。epoch 重置清除上一条目标；发布失败不推进该参考。

拒绝动作窗口时先关闭发送门，再记录 `window_rejected`：实测双臂状态、上一条已发目标、新窗口首个目标、失败检查、侧别、零基关节索引、差值与阈值，以及 inference / EXPO bundle / selected_index 身份。`action_index` 是选中窗口内的零基动作行号，不是关节号。正常路径不增加逐动作日志；此检查不改变 d、C、候选数或模型参数。
