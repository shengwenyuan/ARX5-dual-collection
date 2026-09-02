# Station Initialization 实施计划

- Status: `w4-accepted`
- Parent: `meta_plan.md`
- Final validation station: `w4-arx5`
- Dependency: 双踏板 Trigger 已在 W3 验收

## 目标

提供唯一的交互式站点初始化入口：

```text
arx5-collect station configure
```

该入口发现并验证双臂 USB2CAN、三颗 D405 和两只踏板，完成逻辑角色绑定后原子写入：

```text
/var/lib/arx5-collection/station.json
```

`arx5-collect run` 默认读取该文件。生产代码、Docker、Compose、默认参数和仓库配置不再绑定 W3；W3 历史计划与验收文档保留真实细节。

## 总体边界

- 一台采集主机只维护一个当前有效 Station 配置，不提供仓库内的真实站点 profile。
- 仓库仅保留无真实编号的 schema 示例和测试 fixture。
- 初始化必须连接全部真机并完成实际链路验证；只输入贴纸编号不能构成成功。
- 配置中断、识别模糊或任一设备验证失败时，不覆盖原有效文件。
- 配置使用同目录临时文件、`fsync` 和原子 rename 提交；不生成 SHA。
- CLI 只负责交互和渲染，未来 UI 按钮调用同一个 `StationInitializationService`，不执行 Shell 拼接。

## 最终 Station 配置

新建的 schema v3 至少包含：

```text
station_id
ros_domain_id
sdk_type
arms
  left  -> USB2CAN serial, can1
  right -> USB2CAN serial, can3
cameras
  left, overview, right -> D405 serial
triggers
  activate -> pedal VID/PID/serial
  abort    -> pedal VID/PID/serial
```

- `station_id` 默认使用主机名并允许用户在提交前修改；它是外部命名，项目不解释其格式。
- `ros_domain_id` 由用户显式输入，合法范围为 `0..232`，不从 `station_id` 推导，也不由项目跨工作站分配或查重。
- task description 与 BOS 安全目录段由 collection 配置统一维护，不再修改 station.json。
- 多工作站之间不复制 station.json；每台工作站独立完成设备绑定。
- 初始化在启动任何 ROS 进程前应用该 Domain ID；普通采集与 DAgger 随后只从 Station 配置继承。
- schema v1/v2 可读取和迁移；生产启动缺少 `ros_domain_id` 时明确失败。旧 schema v4 仍可读取。
- 默认 `can1/can3` 来自 environment 的 provisional CAN interface 配置。
- 所有硬件序列号在各自类别内必须完整、非空、唯一，并与当前探测结果一致。
- 运行期每次启动仍重新验证配置与实物，不因初始化成功而跳过设备检查。

## ARX5 / CAN 绑定

两个同型号 USB2CAN 无法仅凭枚举结果判断物理左右，初始化使用真实关节变化识别：

```text
发现两个 USB2CAN
  -> 创建受管临时 CAN 接口
  -> 启动两侧官方 remote_master
  -> 确认状态流和重力补偿
  -> 提示用户轻微移动左臂
  -> 根据关节状态变化识别左侧 USB2CAN
  -> 剩余设备绑定右臂
  -> 生成 left=can1, right=can3
```

- 不制定目标位姿、不录制 Episode，只读取用户主动移动产生的状态变化。
- 重力补偿是识别前置条件；任一侧未进入安全可操作状态则停止。
- 变化量不足、两侧同时明显变化或判断置信度不足时提示重试，不猜测、不写配置。
- 临时 CAN、Vendor 进程和重力补偿资源全部进入受控进程组；成功、失败和 Ctrl+C 都完整回收。
- W3 的已知映射只作为算法开发与回归参考，不作为 W4 默认值或序列号种子。

## D405 绑定

固定按 `left -> overview -> right` 处理三颗相机：

1. librealsense 枚举全部 D405，读取序列号、型号和 USB link。
2. 每个逻辑角色单独选择并启动一颗尚未使用的相机。
3. 用户可从发现列表选择，也可手动输入贴纸序列号。
4. 手动输入必须严格匹配当前发现链路之一，且不能已被其他角色占用。
5. 对候选相机实际启动当前生产规格 848x480 Color + aligned Depth @ 30 Hz，确认帧可达、Color/Depth 对齐链路成立且 USB3 正常后才接受。

- 不用数组枚举顺序推断空间角色。
- 不降低分辨率或帧率来通过初始化。
- 不发布 IR，不在此阶段增加外参标定；相机空间角色由用户确认。
- 任一候选打不开、只有 USB2、型号不符、编号重复或无真实帧时拒绝提交。

## 双踏板绑定

初始化复用已验收的 hidraw 发现组件：

```text
提示“踩下 1 号踏板（SPACE / activate）”
  -> 捕获发送固定按下报告的设备 VID/PID/USB 唯一号
提示“踩下 2 号踏板（A / abort）”
  -> 捕获另一设备身份
```

- 两次踩动必须来自两个不同、稳定可解析的 USB 设备。
- 只接受已验收的完整 hidraw 按下报告；其他 vendor 报告、重复和抖动不参与绑定。
- 完成后立即回放一次语义核对，不启动 Episode。
- W4 可以使用同型号新踏板，但不得继承 W3 的踏板唯一号。

## 模块边界

```text
src/arx5_collection/collection/station/
  models.py                # schema v3 与严格验证
  inventory.py             # udev、librealsense、hidraw 统一盘点
  arm_identifier.py        # 左臂移动信号与 USB2CAN 角色判断
  camera_identifier.py     # 顺序选择、手工编号校验、真实流验证
  pedal_identifier.py      # 踩动顺序与事件身份捕获
  service.py               # 初始化事务、原子提交、回滚

src/arx5_collection/collection/runtime/
  cli.py                   # station configure 交互适配、run 默认路径
```

- `SystemBringup` 继续拥有 usbfs、slcand 和 CAN 生命周期。
- `RosProcessSupervisor` 继续拥有 Vendor/ROS 子进程。
- Station 初始化通过端口调用这些能力，不复制生产 Shell 命令或建立第二套进程管理。
- EpisodeRuntime、Recorder、Store 和数据清洗模块不依赖初始化实现。

## Docker 与本地存储

- production image 同时包含 configure 和 run 所需的 librealsense、udev 与 ARX5 SDK；踏板读取只使用 Python 标准库。
- 继续使用 `privileged container + host network`，并挂载 `/run/udev:ro`。
- 主机 `/var/lib/arx5-collection/` 以可写 bind mount 进入容器；run 只读使用最终文件。
- Compose 不设置带站点名的 `container_name`、输出路径或配置文件名。
- 从零部署先构建镜像，再运行 `station configure`，成功后才允许启动长生命周期采集 Session。

## 迭代步骤

1. 冻结 schema v3、默认路径、原子 Store 和无 W3 的示例配置。
2. 实现统一 Inventory，覆盖 USB2CAN、D405 和 hidraw，并复用现有探测代码。
3. 实现踏板顺序绑定和 D405 顺序选择/手工编号校验。
4. 实现受控 ARX5 左臂移动识别；真机动作步骤停下，由用户启动并监督。
5. 实现 `StationInitializationService` 和 `arx5-collect station configure`。
6. `arx5-collect devices`、`run`、metadata、Source 和 SystemBringup 统一读取最终模型与默认路径。
7. 删除生产代码、Compose 和默认配置中的 W3 绑定，保留历史文档。
8. 先在 W3 重建配置并做回归，再在 W4 完成真正的从零部署验收。
9. 每轮均按“计划回写 -> 代码部署 -> 测试 Cases -> 链路验收 -> 回写结论”闭环。

## 测试 Cases

- 无设备、设备数量不足/过多、重复序列号和权限不足均不产生最终配置。
- 已有有效配置时，任一步骤失败或 Ctrl+C 后原文件字节不变、临时文件清理完成。
- 两侧状态均不变化、同时变化和变化不足时 CAN 角色判断拒绝提交。
- D405 手输不存在编号、重复编号、USB2、错误型号、打不开或无对齐 Depth 时拒绝。
- 踏板两次输入来自同一设备、缺少唯一号或不产生已验收 hidraw 报告时拒绝。
- 成功配置可被 `devices`、`run` 和 metadata 同一模型读取，不出现字段语义分叉。
- 容器重启、USB 枚举顺序和 `hidrawN` 变化后仍按稳定身份恢复全部角色。
- 默认路径缺失时 `run` 给出明确的先执行 configure 提示。
- schema v2 可在 Collector 停止时通过 `arx5-collect station set-ros-domain-id <id>` 原子升级；不得重新绑定硬件。
- Domain ID 缺失、非整数或超出 `0..232` 时生产 Session 必须在启动硬件前失败。

## W4 从零验收

1. 在全新 W4 工作目录构建 production image，不复制 W3 配置、缓存或报告。
2. 运行唯一 configure 入口；依次完成左臂移动识别、三颗 D405 绑定和双踏板踩动绑定。
3. 检查 `/var/lib/arx5-collection/station.json` 权限、schema、唯一性和真实链路匹配。
4. 重启容器，运行统一 devices 检查，确认不依赖原 USB 枚举顺序。
5. 启动长生命周期 Session，确认双臂重力补偿、三路 RGB-D、双路 ArmState 和 pedal Trigger READY。
6. 用踏板完成至少一条 success、一条 operator abort、再一条 success Episode。
7. 验收八路 MCAP、metadata 设备身份、频率、原子目录和完整资源回收。
8. 回写 W4 真实验收结论；动态配置和报告不提交 Git。

## 验收门槛

- W4 不需要编辑仓库文件即可完成首次配置和正式采集。
- 主线生产逻辑、Compose 和默认配置中不存在 W3 序列号、路径或容器名。
- 用户不需要理解 CAN 编号即可稳定完成左右臂绑定。
- D405 和踏板的手工输入都必须由当前真机链路验证，不能保存未经验证的编号。
- configure 失败可安全重试，成功结果可被全部生产模块直接消费。

## 实施记录

- 2026-08-25：revision `d10ca4a` 的 production/DAgger Collector 已统一部署到 W3/W4；用户通过原子迁移入口将 W3 配置为 schema v3、`station_id=w3`、`ros_domain_id=53`，W4 配置为 schema v3、`station_id=w4`、`ros_domain_id=54`。两台 production Session 均已用新镜像启动；这些值仅是部署验收事实，不进入代码默认值或站点映射规则。
- 双踏板 W3 验收已通过，Station Initialization 已开始实现。
- 已实现 schema v2 原子 Store、统一 Inventory、踏板顺序绑定、D405 顺序绑定与真实 720p RGB-D 验证、左臂移动识别、统一 CLI 和运行期七设备身份复核。
- 仓库真实 W3 配置已替换为无真实编号的 `config/environment/station.example.json`；Compose 已移除 W3 容器名和报告路径，主机配置挂载允许 configure 原子写入。
- W3 实测发现 `/dev/ttyACM1` 是 `ARX_KEY` 而非 USB2CAN；统一发现规则固定校验 `ARX + USB2CAN + cdc_acm`，不按 ttyACM 编号猜测。
- 本地相关测试 `152 passed`；W3 production image `arx5-dual-collection:station-init-20260817` 构建成功，容器内新增模块测试 `13 passed`。
- W3 只读 Inventory 已确认：2 个 USB2CAN、3 个 D405、2 个稳定 hidraw 踏板，并已进入完整 configure 真机流程。
- W3 首次 configure 在右相机被枚举为 USB 2.1 时明确拒绝提交；用户修复上行接线后重试，三颗 D405 均以 USB 3.2 通过真实 RGB-D 启动验证。
- W3 左臂移动识别得到 left=`0045002B5330530320323656`、right=`004E002E5330530320323656`；相机角色与既有实物标记一致。
- W3 双踏板按本次实际踩动顺序绑定 activate=`BF554981`、abort=`BF6D54C4`。最终配置原子写入 `/var/lib/arx5-collection/station.json`，随后 `arx5-collect devices` 七项全部 matched。
- W3 验收结论：失败保护、重试、真实链路验证、角色绑定、原子提交和运行期身份复核均符合计划；代码逻辑无需针对本轮优化，进入 W4 从零部署验收。
- W4 已在全新工作站完成标准 Docker Engine、production 镜像、固定持久化目录和容器运行时部署；Station/踏板相关测试 `28 passed`。
- W4 初始化前只读盘点确认 2 个 USB2CAN、3 个 USB 3.2 D405 和 2 个稳定 hidraw 踏板，且无遗留容器或 CAN 接口。
- W4 `station configure` 已完成左臂移动识别、三颗 D405 真实 720p RGB-D 验证和双踏板顺序绑定；相机 left=`261122270159`、overview=`261022274835`、right=`261022277068`，踏板 activate=`BF6EABE6`、abort=`BF6EA0CA`，最终配置原子提交至 `/var/lib/arx5-collection/station.json`。
- W4 容器重启后的 `arx5-collect devices` 七项全部 `matched=true`。
- W4 已多次成功启动 production Session；两条代表性 success Episode 均完成双踏板控制、八路 MCAP、metadata、原子目录和统一退出，Station 初始化与生产消费链路验收通过。90～150 秒压力与必需流故障注入继续由生产编排计划跟踪。
