# 双臂三相机多位姿标定

更新：2026-09-18。采用“两路 wrist 眼在手上、overview 眼在手外”，世界坐标系固定为 **overview RGB 光学系**。不再使用桌角原点，不做桌面坐标注册。

参考 [UR12e 标定计划](/Users/shengwenyuan/neu/ur12e_collection/docs/m12-calibration/plan.md) 的三阶段拆分；相机角色、运动控制、坐标关系按本项目实现。

## 1. 四类入口，三个阶段

```bash
arx5 cali --left-wrist
arx5 cali --right-wrist
arx5 cali --overview-left
arx5 cali --overview-right
```

每个入口显示简单菜单：`1 示教 / 2 自动采集 / 3 离线计算`，默认 1。也可直接加 `--teach`、`--record`、`--solve`，三个阶段互斥。

| 入口 | 运动臂 | 板固定方式 | 手眼类型 |
| --- | --- | --- | --- |
| `--left-wrist` | 左臂 | 独立固定在桌面 | 眼在手上 |
| `--right-wrist` | 右臂 | 独立固定在桌面 | 眼在手上 |
| `--overview-left` | 左臂 | 刚性固定于左臂末端 | 眼在手外 |
| `--overview-right` | 右臂 | 刚性固定于右臂末端 | 眼在手外 |

每次只运动指定臂，另一条臂保持停放状态。两组 overview 数据连接左右 base；两组之间 overview 支架和两臂底座必须保持不动。换手夹板允许改变板—末端偏移，每轮分别估计。

## 2. 第一步：重力补偿、实时画面、Space 保存

```bash
arx5 cali --left-wrist --teach
```

1. 首次运行输入棋盘内角点列数、行数、准确单格边长（mm），保存到 `setup.json`。先核对实际板面平整、尺度与固定方式。
2. 控制器直接进入重力补偿，打开所选相机画面，不执行 HOME。
3. 用户拖动指定机械臂寻找合适视角。OpenCV 实时标记角点、原点 A、+X/+Y，显示检测完整性、清晰度、覆盖与停稳状态。
   相机 Global Time 暂未稳定时仍显示实时图像，并显示时钟等待提示；这些帧不可用于 Space 采集或自动快照。
   时间有效性仍要求主机接收时间减曝光时间落在 -20～200 ms 内，不通过放宽门限获取样本。
   相机错误与时钟状态记录在本次 `logs/<session>/camera.log`，中断原因会在要求手动归位之前显示。
4. 画面显示绿色候选后，按 **Space** 读取并保存当下双方实际六关节角与检测证据。未检测完整、模糊、板太小、反馈过期、未停稳或重复位姿会拒绝保存并给出原因。
5. **Enter** 检查并完成合法 JSON；**Esc** 保存草稿退出。退出前保持重力补偿，提示手动将双臂归位。

辅助按键：`R` 切换角点编号方向，`V` 保存仅用于过渡的停稳姿态，`Backspace` 撤销最后一点。首版使用棋盘格检测，不默认继承 UR12e 的 AprilGrid。

普通棋盘有对称歧义，须给一个外侧内角点做可辨认的物理 A 标记。按 Space 意味着用户已确认画面 A、+X/+Y 与同一物理编号一致；若检测编号反向，先按 R 调整。回放按该站示教图的角点位置匹配编号，明显偏离则拒绝。这个检查不能被解释为普通棋盘自动具备唯一 ID。

每份路线建议 25 个独立采集点，自动将每第 5 个采集点留作验证，通常得到 20 个训练、5 个验证。完成条件至少 15 训练 + 5 验证，并有非平行轴旋转。覆盖不同距离、倾角和图像位置，单转 J6 或同点连拍不满足条件。

JSON 保存有序路径、双方实际角度、相机／设备身份、安装批次、板几何、FK 模型、图像 profile、速度参数、训练／验证划分和示教证据。允许手工修改后用 `--check` 检查；格式／数值检查不替代现场对完整路径的审查。

重新示教会先备份同名旧路线，再建立新草稿。草稿不参与自动回放；旧路线可从 `routes/history/` 指定恢复。当前版本重新示教从新路线开始，不自动续写退出时的草稿。

## 3. 第二步：限速回放与停稳采集

```bash
arx5 cali --left-wrist --check
arx5 cali --left-wrist --record
```

先离线检查整份路线，再检查设备与安装身份。用户将双臂手动放回第一个记录姿态，程序核对实际反馈后才开启回放；起点不符直接停止，绝不跳转首点或自动 HOME。

运动使用五次平滑插值，每段起终速度和加速度均为零：

- 默认关节速度上限 `0.10 rad/s`（约 5.7°/s），加速度上限 `0.20 rad/s²`。
- JSON 可降低速度，软件硬上限为 `0.20 rad/s`、`0.40 rad/s²`；时间预算不足时需增加显式过渡点。
- 独立 50 Hz 控制循环；检测、显示、PNG 编码和写盘不在控制循环内。
- 检查实际位置、速度、反馈新鲜度、跟踪误差及控制循环超时。控制循环停顿后不追赶轨迹时间。
- 底层专用标定模式只处理六关节目标，保留夹爪已有控制目标；拒绝 HOME 和通用遥控通道。命令超时 250 ms 后锁住命令并请求当前位置保持。

每站依次执行 `MOVE → ARRIVED → SETTLE → CAPTURE → COMMITTED`。默认实际停稳 0.6 秒后采集两秒窗口，选择合格图像并保存原始双臂反馈。图片和数据落盘后才进入下一点。

图像使用 RealSense Global Time；保存设备源时间、主机接收时间及其到单调时钟的关联。机器人源时间目前是 ROS 发布时刻，未冒充电机采样时间。通过稳定窗口、连续反馈和时差门限约束图像—姿态关联，窗口外／陈旧数据拒绝使用。

缺图、模糊、超时、移动、反馈异常或 Esc/Ctrl-C 会停止前进并保留 partial 数据。收尾先停止运动；overview 持板轮次提示用户支撑并取下板，再切重力补偿，提示手动归位后退出。底层服务响应表示请求被接受，真实停止与模式切换仍需硬件验收。

## 4. 第三步：离线计算

```bash
arx5 cali --overview-right --solve
```

默认读取所选角色的最新成功 run，并收集同一安装批次下其他角色的最新成功 run。缺组时只输出部分结果，不能标成全系统完成；也可用 `--runs <run-dir> ...` 明确指定输入。

计算顺序：

1. 从训练图像拟合各路 RGB 内参／畸变。overview 左右两轮使用同一套合并训练内参。
2. 用已标内参和棋盘角点求每帧板位姿，检查正深度、平面多解和重投影质量。
3. 从实际关节角与 URDF FK 分别求两路 wrist 安装外参、overview 相对左右 base 的外参。
4. 使用未参与拟合的检查点验证；输出以 overview 为世界系的变换及报告。

不需要重新跑一遍路径来分别求内参、外参。验证点不进入任一拟合阶段。失败不放宽阈值或覆盖已有结果，保留原始证据和失败报告。

## 5. 坐标与公式

`T_A_B` 把 B 系坐标变换到 A 系。长度使用 m、角度 rad；光学系 X 向右、Y 向下、Z 向前。`E_L/E_R` 为对应 URDF `link6`，不是夹爪 TCP。

```text
A_i = T_base_link6(q_actual_i)
Z_i = T_camera_board_i

wrist：   A_i · X · Z_i = Y
          X = T_link6_camera；Y = T_base_board

overview：X · Z_i = A_i · Y
          X = T_base_overview；Y = T_link6_board
```

采用 PARK 手眼法；调用 OpenCV 时，wrist 传 `A_i`，overview 传 `inverse(A_i)`。[OpenCV 定义](https://docs.opencv.org/4.12.0/d9/d0c/group__calib3d.html)

最终世界系 `W = C_O = overview_optical`：

```text
T_W_C_O = I
T_W_B_L = inverse(T_B_L_C_O)
T_W_B_R = inverse(T_B_R_C_O)

T_W_C_L(t) = T_W_B_L · FK_L(q_L(t)) · T_E_L_C_L
T_W_C_R(t) = T_W_B_R · FK_R(q_R(t)) · T_E_R_C_R
```

保存 wrist 到 `link6` 的固定关系，运行时用实际关节角更新世界位姿。用户无需尺量板的世界坐标或板到夹爪的偏移。

## 6. 默认落盘位置

```text
/var/lib/arx5-collection/calibration/
  setup.json
  routes/
    left-wrist.json
    right-wrist.json
    overview-left.json
    overview-right.json
    history/                         # 被新示教替换的路线
    teaching/<route-id>/              # 示教原图
  runs/<role>/<run-id>/
    route.json
    run.json                         # 状态与观测文件索引
    observations/<pose-id>.json       # 原始反馈、检测和时钟
    images/<pose-id>.png
    control-trace.json
  latest-runs/<role>.json             # 只指向完整成功采集
  results/<result-id>/
    calibration.json                 # 内参、外参、残差及有效范围
    evidence-manifest.json
    evidence/<role>/                 # 原始 run 完整副本
  logs/<session-id>/
```

`--data-root`、`--poses`、`--runs` 支持明确覆盖路径。容器默认可读写 `/var/lib/arx5-collection`，读取仓库 `/workspace`；自定义路径须在容器中可见。JSON 中嵌入 URDF 与映射，离线重算不依赖原模型文件仍在原位置。

支架、底座或相机重新安装后，用 `--teach --new-setup` 创建新批次，重新采集受影响路线；其余路线如需复用，应先独立验证后明确管理，不跨批次静默拼接。板每次重新固定都有新的 `board_mount_id`。

## 7. 部署与代码边界

构建硬件主机上的专用镜像：

```bash
docker compose -f docker/compose.calibration.yaml build calibration
```

主机 `scripts/arx5` 转发到 `arx5-collect cali`。硬件运行需要 Linux、已配置 station、X11 `DISPLAY`，以及当前会话可用的 `XAUTHORITY`；compose 传入显示 socket 和认证文件，不全局开放 X server 权限。

离线求解可在 Mac/Linux 安装 `.[calibration]` 后通过 `arx5-collect cali --<role> --solve --runs ...` 运行。`--check`、`--solve`、`--verify --result ...` 都不打开机器人或相机。

| 模块 | 内容 |
| --- | --- |
| `calibration/cli.py` | 四入口、阶段选择、默认路径与设备配置 |
| `calibration/routes.py`、`storage.py` | JSON、草稿、完整路线校验、原子保存 |
| `calibration/board.py`、`workflow.py` | 实时标记、Space 保存、回放采集与退出 |
| `calibration/motion.py`、`replay.py` | 平滑限速、反馈检查、独立控制循环 |
| `calibration/hardware.py` | 专用 ROS 控制节点和所选 D405 pipeline |
| `calibration/geometry.py`、`solve.py` | URDF FK、两类手眼、overview 世界系、证据重算 |
| `production/lease.py` | collect／DAgger／站点初始化／标定共享的硬件锁 |
| `docker/patches/arx-x5-calibration-control.patch` | 标定专用保持、命令守卫、超时与夹爪隔离 |

标定会话独占 CAN 和相机，使用单独的所选相机 RealSense pipeline；不会同时启动生产相机源，也不改变生产 8/5 路 MCAP 契约。相机保持原生 `848×480@30 RGB8`，保留工厂深度／RGB 内参、内部外参和 depth scale 元数据；RGB 标定不修改固件或 SDK depth alignment。

## 8. 验收范围

软件检查包括：位姿 JSON 拒绝错误／退化数据，五次轨迹速度与加速度界限，反馈故障停止，两类手眼已知变换恢复，overview 世界变换方向，训练／验证分离，原始图像与 FK 的离线重算、证据篡改检测和硬件互斥。

初始数值门限分别为板位姿一致性 `2 mm / 1°`、图像 PnP 重投影 `1 px`，报告每点及验证集 P95／最大误差；同时单独报告固定手眼模型预测的像素误差。这是软件一致性门限，不是绝对毫米精度承诺。`absolute_accuracy_verified` 和 `cross_arm_independently_verified` 默认 false，必须有独立物理参考后才能改为已验证。

已随包保存用户提供的 X5 URDF；默认关节顺序 `joint1..joint6`、符号 +1、偏移 0，可用 `--mapping` 提供六维 `signs`、`offsets`。现场仍须核实实际反馈与这个模型的零位／轴向一致；CAD 外参不代替视觉求解，视觉手眼也不校正全部连杆和零偏误差。

现场验收还包括：控制器启动与退出、重力补偿、夹持保持、显示与相机时钟、真实速度和停止效果、棋盘可见性、整条路径以及独立毫米误差。软件测试或合成数据通过不代表这些硬件项已验收。结果尚未自动激活到生产 episode；保存和离线验证是本轮交付边界。

本轮软件验证（2026-09-18）：全仓 `pytest -q tests` 为 **327 passed, 1 skipped**；wheel 构建通过并包含 X5 URDF；compose 配置解析通过；控制补丁可应用到固定版本源码，新增 C++ 守卫方法通过独立编译测试。当前 Mac Docker daemon 未运行，未完成完整 ROS/SDK 容器构建，也未执行真机标定或运动。
