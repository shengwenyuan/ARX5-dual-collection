# Episode 开始前自动归位实施计划

- Status: `awaiting-hardware-revalidation`
- Parent: `meta_plan.md`
- Branch: `feature/post-episode-reset`
- Safety boundary: 真机位姿变化测试由用户现场启动和监督

## 目标

把 Vendor `GO_HOME` 作为每条 Episode 的开始前置动作，而不是结束动作。用户在 `READY` 按下 `SPACE` 后：

```text
SPACE
  -> 双臂同时 GO_HOME，夹爪闭合
  -> 等待关节位置与速度稳定收敛
  -> 双臂恢复 G_COMPENSATION
  -> Recorder start
  -> RECORDING
```

- GO_HOME 与恢复重力补偿期间 Recorder 不运行，不把自动归位轨迹写入 Episode。
- 只有左右臂均确认进入重力补偿后才允许启动 Recorder，避免机械臂保持或掉落。
- Episode 由 `SPACE`、`A`、必需流异常或录制中 `Ctrl+C` 结束后均不自动归位。
- 空闲 `Ctrl+C` 只关闭 Session；录制中 `Ctrl+C` 先提交 aborted Episode，再关闭 Session。

## 控制边界

- 使用 `ARXroboticsX/ARX_X5 main@c783287` 的 `GO_HOME` 与 `G_COMPENSATION` 状态。
- Vendor 默认 home 为左右臂相同的 6 关节目标；夹爪闭合使用官方示例值 `-1.0`。
- 使用 Vendor 默认 `GO_HOME` 运动曲线，不伪造不存在的 10% 限速接口。
- 双臂归位服务同时请求，不通过全局 `/arx_joy`。
- 六关节在官方 home 目标 `0.03 rad` 内、速度在 `0.05 rad/s` 内连续稳定 `0.5 s` 后，才同时请求重力补偿；总超时 `45 s`。
- 任一归位、收敛或重力补偿请求失败时不启动 Recorder，并关闭 Session。

## 模块边界

```text
src/arx5_collection/reset/
  coordinator.py                  # 归位状态与 Controller 调用

src/arx5_collection/ros2_adapters/reset.py
                                  # 双臂服务、状态收敛、恢复重力补偿

docker/patches/arx-x5-go-home-services.patch
                                  # Vendor 私有 GO_HOME/G_COMPENSATION 服务

ProductionSession.create_runtime
  pre_episode_check
  -> pre_recording_action
  -> EpisodeRuntime 启动 Recorder
```

- CLI 只渲染复位状态，不承载运动步骤。
- Episode Runtime 不 import Vendor SDK。
- 复位逻辑与离线数据清洗无关。

## 验收

- 每次 `SPACE` 只触发一次开始前归位。
- Recorder start 严格晚于 `RESET_COMPLETE` 和双臂重力补偿成功响应。
- 自动归位轨迹不进入 MCAP。
- 正常结束、`A`、设备异常与 `Ctrl+C` 均不触发结束后归位。
- 空闲 `Ctrl+C` 不发送任何位姿变化信号。
- 下一条 Episode 只有再次按 `SPACE` 才重新归位。

## 实施记录

- 官方节点补丁已增加 `/arm_master_l|r/go_home` 与 `/arm_master_l|r/gravity_compensation`。
- 第一轮结束后归位方案已由真机验证可运动，但确认 `GO_HOME` 不适合作为退出安全状态，因此废止。
- 当前最小修改将相同 Controller 移至 Recorder 启动前，移除 Episode 结束和 Ctrl+C 的归位回调，并移除原 5 秒等待。
- 第一轮纯软件验收为 `120 passed, 16 subtests passed`；w3 production 镜像 `arx5-dual-collection:post-reset-337c6ca` 构建成功。
- 本轮纯软件验收为 `117 passed, 16 subtests passed`；顺序测试确认归位与重力补偿完成早于 Recorder start。
- w3 独立 feature 镜像 `arx5-dual-collection:post-reset-df012c1` 构建成功；无设备静态检查确认生产入口不存在结束后归位回调。
- 下一步由用户监督真机复验 SPACE 前置归位、重力补偿保持和 Ctrl+C 单纯退出。
