# Episode 后自动复位需求

- Status: `draft-alignment`
- Parent: `meta_plan.md`
- Trigger: 正常 `SPACE` 结束并成功提交 Episode
- Safety: 目标位姿、运动路径和 Vendor 控制接口确认前禁止真机下发

## 目标

每条正常 Episode 完成后，系统等待 5 秒，再以低速将双臂移动到经过验证的标准初始位姿，并确认夹爪完全闭合。复位完成且状态稳定后才重新进入 `READY`，保证下一条 Episode 从一致状态开始。

该需求首次突破当前“采集程序只读取机械臂状态”的冻结边界。实现前必须同步更新 `meta_plan.md`，并建立独立、可取消、可验收的运动控制边界。

## 建议状态流

```text
RECORDING
  --SPACE--> FINALIZING
  --> SUCCESS_COMMITTED
  --> RESET_PENDING (5 s countdown)
  --> RESETTING
  --> RESET_VERIFYING
  --> READY

RESET_PENDING / RESETTING
  --cancel or safety failure--> RESET_BLOCKED

RECORDING
  --A / source failure / Ctrl+C--> ABORTED or SESSION EXIT
  --> no automatic reset
```

- 复位属于生产 Session 的 Episode 后处理，不属于 Recorder，也不写入刚结束的 MCAP。
- Episode Core 仍只负责录制与原子提交；Production 层在收到 committed `success` 后执行 `PostEpisodeReset`。
- 复位未成功时不得显示 `READY`，不得开始下一条 Episode。
- `RESET_BLOCKED` 只能由明确的人工处理恢复，不能静默跳过。

## 安全边界

- 目标必须是已由用户在真机验证的双臂关节位姿和夹爪闭合值；不得由代码猜测、从 URDF 默认值推导或自动创建新位姿。
- 优先使用关节空间目标与经过验证的安全路径，不直接猜测 EEF 直线路径。
- 速度、加速度、超时和关节容差必须写入站点 Reset 配置，并受 Vendor 官方上限约束。
- 启动运动前必须确认双臂状态新鲜、CAN 无错误、Controller 健康、当前无 Recorder、没有下一条 Episode。
- 5 秒等待期必须显示明确倒计时和取消方式；自动运动不能无提示发生。
- 运动期间必须能调用 Vendor 支持的 stop/hold；仅杀死进程不能作为正常急停方案。
- 任何通信中断、状态停更、超时、超差或取消都立即停止复位并进入 `RESET_BLOCKED`。
- 软件取消不能替代真机急停；首次开发和验收必须由用户在设备旁操作。

## 标准位姿配置

建议将标准位姿作为 w3 站点配置的独立版本化对象：

```text
reset_pose:
  version
  left_joint_positions[6]
  left_gripper_closed
  right_joint_positions[6]
  right_gripper_closed
  optional_verified_waypoints
  velocity_scale
  acceleration_scale
  timeout_s
  joint_tolerance
  stationary_velocity_tolerance
  stable_duration_s
```

建议提供只读状态采样命令，由用户手动摆好双臂并闭合夹爪后捕获候选数值；候选值必须人工检查并明确确认后才能成为生产配置。采样命令本身不发送运动指令。

## 模块边界

```text
src/arx5_collection/reset/
  models.py       # ResetPose、ResetState、ResetResult
  ports.py        # ArmResetController、SafetyGate
  coordinator.py  # countdown、顺序、验证、取消
  config.py       # 站点 Reset 配置严格解析

ros2_ws/src/arx5_reset_adapter/
  ...             # 仅封装确认后的 ARX5 官方控制接口
```

- CLI 只展示倒计时、状态和错误，不包含运动步骤。
- Coordinator 不直接 import Vendor SDK，通过最小 Controller Port 调用。
- 不使用 Shell 拼接运动命令，不通过 `/arx_joy` 注入控制。
- 复位逻辑与数据清洗完全无关。

## 实施步骤

1. 查明 ARX5 官方 ROS 2/SDK 中低速关节目标、夹爪闭合、stop/hold、完成反馈和重力补偿模式切换的真实接口。
2. 用户手动摆放并捕获标准双臂关节位姿与夹爪闭合值，冻结 w3 Reset 配置。
3. 使用 Fake Controller 完成状态流、5 秒倒计时、取消、超时、错误和稳定判定测试。
4. 接入单臂、无负载、低速真机测试；由用户现场确认启动与停止。
5. 验证安全路径后测试双臂复位；确认复位过程不写入上一 Episode。
6. 连续多条 Episode 验收 `success -> reset -> READY`，失败时必须停在 `RESET_BLOCKED`。
7. 验收完成后再更新 `meta_plan.md` 的只读边界和正式状态图。

## 待对齐决策

1. 5 秒从按下结束 `SPACE` 开始，还是从 MCAP/metadata 成功提交后开始。
2. 自动复位是否只用于 committed `success`；`A`、设备异常和录制中 `Ctrl+C` 是否永不自动复位。
3. 标准位姿是否由用户手动摆好后通过只读命令捕获六关节与夹爪值。
4. Episode 结束时夹爪可能持有物体：复位前由人清空，还是需要“释放物体”动作；不得直接闭合并携带未知物体运动。
5. 双臂同时移动还是按固定顺序逐臂移动；是否需要已验证中间 waypoint。
6. “慢速”的速度/加速度比例、最大复位时间、到位容差与稳定时长。
7. 5 秒等待和运动中的取消键，以及取消后如何人工恢复。

