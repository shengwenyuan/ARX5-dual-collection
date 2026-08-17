# 双踏板 Trigger 实施计划

- Status: `accepted-for-main`
- Parent: `meta_plan.md`
- First validation station: `w3-arx5`
- Dependency: 当前 `RecordTrigger`、长生命周期 `ProductionSession`

## 目标

将两只独立 LinkStone 连拓 A096H USB 踏板接入现有 Trigger 边界：

- 1 号踏板完全替代 `SPACE`：`READY` 时开始 Episode，`RECORDING` 时以 `success` 结束。
- 2 号踏板完全替代 `A`：仅在 `RECORDING` 时以 `operator requested abort` 结束当前 Episode，提交到 `aborted/` 后返回 `READY`。
- 保留键盘作为踏板不完整时的整组回退，不改变 Episode 状态机、Recorder、Store 或 MCAP 语义。
- 踏板属于站点硬件，其身份最终写入 `/var/lib/arx5-collection/station.json`，不进入 Git。

## 已确认设备事实

W3 上两只踏板均由 Linux `usbhid` 正常驱动，不需要厂商内核驱动。真实踩踏从 vendor-defined hidraw interface 发送固定 64 字节报告：

| 角色 | 型号 | VID:PID | USB 唯一号 |
| --- | --- | --- | --- |
| activate | A096H | `8088:0015` | `BF6D54C4` |
| abort | A096H | `8088:0015` | `BF554981` |

按下报告固定为 `66cc030001` 加 59 个零字节。`hidrawN` 和 USB 物理端口都可能变化，不得作为持久身份；运行时按 VID、PID、USB 唯一号和 vendor report descriptor 重新解析。

## Auto 规则

生产入口只提供一种自动选择行为，不增加 `pedal-only`、`keyboard-only` 等用户入口：

```text
Session startup
  -> 两只已配置踏板均存在、唯一且可读 -> PedalTrigger
  -> 任一缺失、重复或不可读          -> KeyboardTrigger(SPACE/A)
```

- 不使用“一只踏板 + 一个键盘按键”的混合模式。
- 输入 Provider 只在 Session 启动时选择一次，运行中不热切换。
- 回退时必须明确打印原因与 `TRIGGER_MODE=keyboard-fallback`；踏板可用时打印 `TRIGGER_MODE=pedal`。
- 键盘回退仍要求交互式 TTY；没有 TTY 时明确失败，不创建 Episode。
- 踏板在 `READY` 期间断连，Session 失败退出；录制中断连，当前 Episode 以设备异常 `aborted` 提交后退出 Session。
- 同一轮同时出现两种信号时，`ABORT` 优先于 `ACTIVATE`。

## 输入语义

- 只响应完整匹配的 64 字节 hidraw 按下报告；其他 vendor 报告一律忽略。
- 每只踏板独立防抖，初始窗口为 `200 ms`。
- `READY` 中忽略 2 号踏板；它不能跳过归位、启动 Recorder 或退出 Session。
- 1 号踏板开始 Episode 后，仍先完成双臂 GO_HOME、稳定收敛与重力补偿，再启动 Recorder。
- 读取时按站点配置中的 VID、PID、USB 唯一号解析当前 vendor hidraw 节点，不依赖 Keyboard/Mouse interface。

## 配置边界

站点 schema v2 增加 Trigger 身份；不保存易变的 `/dev/hidrawN`：

```json
{
  "triggers": {
    "activate": {
      "vendor_id": "8088",
      "product_id": "0015",
      "serial_number": "<detected>"
    },
    "abort": {
      "vendor_id": "8088",
      "product_id": "0015",
      "serial_number": "<detected>"
    }
  }
}
```

正式的交互式绑定由后续 Station Initialization 统一提供。本阶段仅在 W3 读取真实事件后，人工生成一份完整本地配置用于链路验收；真实编号不得写入仓库模板或业务代码。

## 模块边界

```text
src/arx5_collection/episode/adapters/
  pedal.py                 # hidraw 解析、两个 fd、防抖、ABORT 优先

src/arx5_collection/production/
  triggers.py              # AutoTriggerFactory 与启动检查/回退原因
  config.py                # station schema v2 TriggerConfig

EpisodeRuntime             # 不修改 TriggerEvent 和状态机语义
ProductionSession          # 每个 Session 只创建一个 Trigger Provider
```

- 只使用 Python 标准库读取 hidraw，不增加第三方输入依赖，不在 CLI 中搬运 Shell 或手写业务状态机。
- CLI 只显示当前 Provider 与异常；设备解析、回退和事件语义均由独立对象承担。
- 不把踏板轮询并入 ROS executor，不引入 ROS Topic，也不把触发事件录入 MCAP。

## 迭代步骤

1. 在 W3 采集两只踏板的原始 hidraw 报告，并通过踩动顺序确认 1/2 号身份。
2. 扩展站点配置模型和测试 fixture，加入严格唯一的双踏板身份。
3. 实现 `PedalTrigger` 与 `AutoTriggerFactory`，保持 `RecordTrigger` 接口不变。
4. 将生产入口从直接构造 `KeyboardTrigger` 改为构造 Auto Provider。
5. 在 W3 的 `/var/lib/arx5-collection/station.json` 人工写入已确认映射，重新构建并部署 production image。
6. 完成软件 cases、真机信号测试、完整 Episode 链路测试，并把结论回写本文件。

## 测试 Cases

- 两踏板都存在时选择 `pedal`，键盘输入不参与触发。
- 任一踏板缺失、重复、身份错配或不可读时，整组选择 `keyboard-fallback`。
- 无 TTY 且需要键盘回退时启动失败。
- 1 号按下在 `READY` 只启动一次，在 `RECORDING` 只成功结束一次；重复和抖动不产生额外事件。
- 2 号按下在 `READY` 无效，在 `RECORDING` 产生一次 `ABORT`，提交 aborted Episode 并继续 Session。
- 两踏板同时按下时只产生 `ABORT`。
- 录制中断开踏板时提交设备异常 aborted Episode，Session 非零退出且完整回收 ROS、CAN 和文件句柄。
- 连续 Episode 之间不重启踏板、CAN、相机或 ROS Source。

## W3 链路验收

1. 启动 Session，确认打印 `TRIGGER_MODE=pedal`。
2. 用 1 号踏板完成一条 `start -> success` Episode。
3. 用 1 号开始、2 号结束一条 `aborted` Episode，确认 Session 回到 `READY`。
4. 再用 1 号完整录制一条 success Episode，证明 abort 后可继续。
5. 检查三条 Episode 的目录、metadata、八路 MCAP、频率和结果分类。
6. 停止 Session 后确认无容器子进程、CAN、hidraw fd 或 partial Episode 残留。
7. 临时移除一只踏板，重启 Session 并确认整组回退 SPACE/A；复原后重新启动恢复 pedal 模式。

## 验收门槛

- 所有 Trigger 软件测试通过，现有 Episode/Production 回归测试无退化。
- 三条真机 Episode 的开始、success、abort 和继续行为与键盘语义完全一致。
- 自动回退可观察、无混合输入、无运行中静默切换。
- W3 真实踏板编号只存在于主机本地 Station 配置和动态报告中。

## 实施记录

- 已实现 station schema v2 双踏板身份、`PedalTrigger`、`AutoTriggerFactory`、启动期整组键盘回退、断连失败和生产 CLI 接入。
- 第一版 evdev 方案只能看到设备模拟键盘能力，实际踩踏没有 `EV_KEY`；继续核查 vendor hidraw interface 后，确认两只设备均发送相同的固定 64 字节按下报告。
- 已冻结 W3 物理角色：`BF6D54C4=activate`，`BF554981=abort`。
- 已删除 `python3-evdev`、事件类型/键码配置、Keyboard/Mouse interface 匹配和 exclusive grab；运行时仅使用 Python 标准库解析 hidraw。
- 轻量重构后的本地全量回归为 `150 passed`，W3 隔离部署树定向测试为 `21 passed`。
- W3 镜像 `arx5-dual-collection:pedal-hidraw-20260817` 构建成功并部署为 `production`；本地 schema v2 配置写入 `/var/lib/arx5-collection/station.json`，未进入 Git。
- 2026-08-17 真实主线 smoke 输出依次为 `TRIGGER_MODE=pedal`、`PASS activate`、`PASS abort`、`PASS cleanup`，证明 Auto 选择、两只踏板语义和 fd 回收通过。
- 本轮按用户确认允许合入 main。未单独录制 success/abort/success MCAP；该联合回归并入后续 Station Initialization 的 W3/W4 链路验收，不把信号层 smoke 伪装成八路 Episode 验收。
