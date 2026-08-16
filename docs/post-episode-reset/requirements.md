# Episode 后自动复位实施计划

- Status: `blocked-alignment`
- Parent: `meta_plan.md`
- Branch: `feature/post-episode-reset`
- Safety boundary: 未经用户确认，不启动真机全链路测试或发送位姿变化信号

## 目标

每条 Episode 结束后，无论由 `SPACE`、`A`、必需流异常还是 `Ctrl+C` 触发，都等待 Episode 完成提交，再等待 5 秒，然后让左右臂同时、低速复用 ARX5 官方“开启重力补偿时的归位”动作。归位完成后：

- 正常 Session 继续运行时进入 `READY`。
- `Ctrl+C` 请求退出时，归位完成后再执行原有 Session 关闭与资源回收。

复位期间 Recorder 已停止，不把复位运动写入刚结束的 Episode。

## 冻结状态流

```text
SPACE / A / source failure
  -> FINALIZING
  -> Episode committed
  -> RESET_WAITING (5 s)
  -> RESETTING (left + right concurrently, 10% speed/acceleration)
  -> READY

Ctrl+C
  -> 当前 Episode 若存在则先完成 aborted 提交
  -> RESET_WAITING (5 s)
  -> RESETTING
  -> ordered Session shutdown
```

- `A` 与正常结束后的复位路径一致，仅 Episode outcome 不同。
- 空闲 `READY` 时收到 `Ctrl+C` 也执行 5 秒等待和归位，再关闭 Session。
- 归位失败时不进入 `READY`；返回明确错误并关闭 Session。
- 双臂同时运动，不增加逐臂顺序、避障、物体释放或额外 waypoint。
- 前提简化为：标准位姿安全、工作区无遮挡、夹爪无夹持。

## 官方位姿与控制边界

- 不测量、不配置自定义关节目标。
- 必须查明并复用 ARX5 官方 `remote_master + G_COMPENSATION` 初始化时已经执行的同一归位动作与位姿。
- 不通过 `/arx_joy`、Shell 命令拼接或猜测关节值实现。
- 速度与加速度均以官方允许的比例接口限制为 10%；若官方归位接口不支持速度限制，停止实现并重新对齐，不自行伪造等价动作。
- 使用官方完成反馈或状态条件确认左右臂均归位完成；不能仅按固定时长假定成功。

## 模块边界

```text
src/arx5_collection/reset/
  models.py       # ResetState、ResetResult
  ports.py        # DualArmResetController
  coordinator.py  # 5 秒等待、并发归位、完成/错误

ros2_ws/src/arx5_reset_adapter/
  ...             # 封装确认后的 ARX5 官方归位接口
```

- Episode Runtime 继续只负责录制和提交，不直接 import Vendor SDK。
- Production Session 在每次 Episode 结果之后调用 Reset Coordinator。
- CLI 只显示 `RESET_WAITING / RESETTING / RESET_COMPLETE`，不承载运动步骤。
- 复位逻辑与离线数据清洗无关。

## 实施步骤

1. 只读核查 ARX5 官方源码，确认初始化归位、速度/加速度限制、双臂并发调用和完成反馈。
2. 冻结最小 `DualArmResetController` Port 与 Session 调用点。
3. 使用 Fake Controller 实现并测试 SPACE、A、source failure、录制中 Ctrl+C、空闲 Ctrl+C。
4. 覆盖 5 秒从 Episode 提交后开始、双臂并发、复位失败不 READY、退出时先复位后 shutdown。
5. 构建 Docker/ROS Package 并完成不触发运动的静态检查。
6. 停在真机链路测试前，等待用户明确确认和现场监督。
7. 真机验收后回写本计划与 `meta_plan.md` 的控制边界。

## 首版验收

- 任何 Episode 结果提交后只触发一次复位。
- 5 秒计时不与 FINALIZING 重叠。
- 左右臂归位调用并发开始，均使用 10% 速度/加速度。
- 归位期间不会启动下一条 Recorder。
- `Ctrl+C` 不会绕过复位，归位后原有有序关闭仍完整执行。
- 无真机确认时，自动化测试不得调用真实 Controller。

## 已对齐决策

- 5 秒从 Episode 成功或异常提交完成后开始。
- `SPACE`、`A`、设备异常和 `Ctrl+C` 均自动复位。
- 复用官方重力补偿初始化归位，不采集或维护自定义目标位姿。
- 双臂同时运动；速度与加速度比例为 10%。
- 假设工作区安全、无遮挡且无夹持，不扩展复杂防碰撞逻辑。
- `Ctrl+C` 先等待并归位，再关闭整个 Session。
- 全链路真机测试必须等待用户再次明确确认。

## 官方源码核查

核查基线：`ARXroboticsX/ARX_X5 main@c783287`。

- `remote_master` 启动时直接调用 `G_COMPENSATION`；显式归位是另一个 `GO_HOME` 状态。
- 官方 `v2_collect.yaml` 给左右臂配置同一组 6 关节 `go_home_position`，不包含夹爪目标。
- 当前 ROS 2 节点只通过全局 `/arx_joy` 触发 `GO_HOME`，没有独立服务。
- 公开接口没有归位速度、加速度或完成反馈参数；状态 Topic 仅能用于外部判断关节位置和速度是否收敛。
- 因此当前无法同时严格满足“不使用 `/arx_joy`、官方接口限速 10%、官方完成反馈、夹爪完全闭合”。在重新对齐控制边界前，不实现真实 Controller Adapter。
