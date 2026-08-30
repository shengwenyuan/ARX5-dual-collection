# 叠衣服 20260828 DAgger Profile

## 目标

将 W3 已验证的 `pi05-fold-iter-swy` training-time RTC 模型作为新的并列 DAgger profile 接入，不改变 Policy Server、RTC Scheduler、Take-over 状态机或 Episode 链路。

## 冻结配置

- checkpoint：`fold_cloth_20260825_20260826_pi05_train_rtc_v1/27999`
- checkpoint SHA-256：`5c2248749f3eaa21f7a6cf2652c3d1306771aa572f1814e51b12c9e58cda38fb`
- prompt：`folding the cloth`
- action：14 维绝对关节动作，50-step horizon，10-step 执行窗口
- RTC：10 flow steps，10-step 最大延迟，hard prefix，rolling-max delay
- 控制频率：30 Hz
- 输入：三路 RGB，`640x360 -> 224x224 resize_with_pad`
- 夹爪：沿用 `arx5-gripper-v1`，左右归一化夹爪值增加 `0.1` 后，在 `[0, 1.11]` 内执行，与独立 inference 的额外闭合预紧语义对齐

## 实现边界

新增独立 TOML profile，并为既有夹爪动作契约增加一个缺省为 `0.0` 的 `normalized_action_offset`。RTC hard-prefix 保留模型原始动作；仅在发布命令前增加夹爪偏移，并在 profile 的 `[0, 1.11]` 边界内沿标定直线外推，与独立 inference 保持一致。数据归一化仍严格保持 `[0,1]`。保留当前 DAgger 安全阈值和故障处理，不复用 W3 的 shell 入口，也不新增模型专用分支。

## 验收

先完成配置加载、夹爪映射和 RTC prefix 单元测试；本轮不启动模型、不发送机械臂控制命令。之后在 W3 由用户使用统一 `arx5 dagger` 入口进行真机验收。
