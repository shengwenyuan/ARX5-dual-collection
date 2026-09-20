# 双臂三相机多位姿标定

更新：2026-09-20。采用“两路 wrist 眼在手上、overview 眼在手外”，世界坐标系固定为 **overview RGB 光学系**。不再使用桌角原点，不做桌面坐标注册。

参考 [UR12e 标定计划](/Users/shengwenyuan/neu/ur12e_collection/docs/m12-calibration/plan.md) 的三阶段拆分；相机角色、运动控制、坐标关系按本项目实现。

## 1. 四类入口，三个阶段

```bash
arx5 cali --left-wrist
arx5 cali --right-wrist
arx5 cali --overview-left
arx5 cali --overview-right
```

每个入口显示简单菜单：`1 示教 / 2 自动采集并计算 / 3 离线重算`，默认 1。也可直接加 `--teach`、`--record`、`--solve`，参数互斥；`--record` 遍历后在归位、关闭硬件之后自动执行离线计算，`--solve` 保留为已有数据重算入口。

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
3. 用户拖动指定机械臂寻找合适视角。OpenCV 实时标记角点、原点 A、+X/+Y；右侧英文校验面板每项独立显示绿色 PASS 或橙色 WAIT，全部通过后才可保存。
   相机 Global Time 暂未稳定时仍显示实时图像，并显示时钟等待提示；这些帧不可用于 Space 采集或自动快照。
   时间有效性仍要求主机接收时间减曝光时间落在 -20～200 ms 内，不通过放宽门限获取样本。
   相机错误与时钟状态记录在本次 `logs/<session>/camera.log`，中断原因会在要求手动归位之前显示。
4. 画面显示绿色候选后，按 **Space** 读取并保存当下双方实际六关节角与检测证据。未检测完整、模糊、板太小、反馈过期、未停稳或重复位姿会拒绝保存并给出原因。
5. **Backspace** 删除上一条并立即落盘；**关闭窗口** 自动检查路线，满足数量及可观测性要求则保存为可回放 JSON，否则保留草稿并在终端说明。退出前保持重力补偿，提示手动将双臂归位。示教窗口只使用 Space 和 Backspace 两个按键，终端归位确认仍需 Enter。

Space 是逻辑上的增量保存：把新位姿加入 `waypoints` 列表，再将完整 JSON 写入同目录临时文件，
flush/fsync 后原子替换正式文件；不是向文件尾追加，也不是 JSONL。关闭窗口时完成合法性检查及状态更新。
重新运行 `--teach` 会归档旧 JSON 并新建路线，不自动续写上一轮。

不再使用 R / V / Enter / Esc 示教快捷键，也不新增仅过渡点；相邻位姿距离过大时补采中间视角。旧 JSON 的 via 点仍可回放。

普通棋盘有 180° 编号歧义，方形棋盘还存在 90° 歧义。示教中的 A/+X/+Y 只帮助观察，不携带可跨站复用的物理 ID。回放不与任何示教像素比较。求解时先验证内参，再用机器人相对旋转角不变量确定训练图像的一致编号；验证图像只对已固定的训练编号判别，不反过来更新训练拟合。不能可靠区分时报告歧义并判定 invalid，不默认为相同物理原点。

实时逐项校验：角点完整、覆盖率（默认 ≥1.5%）、清晰度（≥40）、边缘距离（≥8 px）、相机时钟、图像时效；左右臂分别显示反馈时效（≤100 ms）、关节限位和静止速度（≤0.04 rad/s）；双臂连续停稳（≥0.6 s）、停稳后曝光、与已有位姿的差异、非示教臂是否保持原位、相邻运动段时长。某项不通过不影响其他项显示绿色。预览底部只保留 Space / Backspace 操作行，保存与拒绝原因在终端输出。

静止速度门槛按现场报告的 0.01–0.03 rad/s 波动调整为 0.04 rad/s；这不是运动指令速度，也不是末端定位精度。连续停稳仍要求 0.6 秒，关节位置相对停稳基准的变化仍须 ≤0.01 rad。`Stable dwell` 显示计时及归零原因（哪侧速度超限、位置变化或反馈缺失）。`Post-settle exposure` 自动等待停稳条件和对应曝光帧，无需用户输入。新示教使用新默认值；已有 JSON 保留其记录的 motion 参数，历史 run 不自动改写。

反馈读取在棋盘检测之后执行，Space 按下时再读取并复核，不用检测前的旧状态判断超时。仅示教对短暂的关节反馈接收超时暂停保存并重新计停稳时间；连续 2 秒没有恢复则中断。控制器退出、非有限反馈等真实故障不被此恢复逻辑吞掉。自动回放反馈时效、控制线程和运动保护继续严格生效，不放宽阈值。

每份路线建议示教 35 个以上候选点，目标是在不同工作台仍获得 20 个以上有效观测；35 个目标不保证必然得到 20 个有效点。路线完成条件为至少 20 个不同候选点，并具备非平行轴旋转。覆盖不同距离、倾角和图像位置，单转 J6 或同点连拍不满足条件。

**schema 2 路径 JSON 可以跨工作台复用**，只保存角色、有序双方关节目标（rad）、FK 模型与关节映射、原生图像 profile、运动参数及草稿状态。不再保存 station ID、设备/相机序列号、安装批次、棋盘尺寸、示教像素或训练/验证划分。图片与当时的关节反馈另存于 `routes/teaching/<route-id>/`，不作为回放条件。

复用前提是 X5 型号、关节顺序/符号/零偏映射一致，完整路径在目标工位可执行；仅复制关节路径不能保证相机可见性或避碰。站点设备身份、棋盘尺寸与安装批次由本站配置提供，写入新 run。旧 schema 1 路线在读取时转换为 schema 2，不改关节值或顺序，也不将旧标定结果当成本站结果。

重新示教会先备份同名旧路线，再建立新草稿。草稿不参与自动回放；旧路线可从 `routes/history/` 指定恢复。当前版本重新示教从新路线开始，不自动续写退出时的草稿。

## 3. 第二步：限速回放与停稳采集

```bash
arx5 cali --left-wrist --check
arx5 cali --left-wrist --record
```

先离线检查整份路线，再核对本站机械臂模型/关节映射及本站设备配置。启动时检查双臂反馈、静止状态、关节限位以及全部起步运动段。无需手动对齐首姿：程序先依次将左、右臂限速移动到 HOME，确认双臂停稳后，先将非采集臂移到路线记录的停放位，再将采集臂移到首姿；每段均等待实际到位和连续停稳，之后才开始快照。

HOME 与 collection 共用 `DEFAULT_HOME = [0, 0.948, 0.858, -0.573, 0, 0] rad`。通过标定六关节轨迹执行，沿用同一速度/加速度限制，不调用会改变夹爪目标的普通 `go_home` 服务。夹爪保持已有目标，overview 棋盘不需因 HOME 松开。操作者只需清空 HOME 及首姿接近路径并松开双臂，不再需要人工将各关节摆到 0.57° 内。启动阶段画面显示 HOME / START，可 Esc 或关闭窗口中止。

运动使用五次平滑插值，每段起终速度和加速度均为零：

- 默认关节速度上限 `0.10 rad/s`（约 5.7°/s），加速度上限 `0.20 rad/s²`。
- JSON 可降低速度，软件硬上限为 `0.20 rad/s`、`0.40 rad/s²`；时间预算不足时需增加显式过渡点。
- 独立 50 Hz 控制循环；检测、显示、PNG 编码和写盘不在控制循环内。
- 检查实际位置、速度、反馈新鲜度、跟踪误差及控制循环超时。控制循环停顿后不追赶轨迹时间。
- 底层专用标定模式只处理六关节目标，保留夹爪已有控制目标；拒绝会改变夹爪目标的普通 HOME 服务和通用遥控通道；自动 HOME 使用标定轨迹通道。命令超时 250 ms 后锁住命令并请求当前位置保持。

每个路径点依次执行 `MOVE → ARRIVED → SETTLE → CAPTURE → CAPTURED / SKIPPED`。默认实际停稳 0.6 秒后采集两秒窗口，选择合格图像并保存原始双臂反馈。图片和数据落盘后才进入下一点。

图像使用 RealSense Global Time；保存设备源时间、主机接收时间及其到单调时钟的关联。机器人源时间目前是 ROS 发布时刻，未冒充电机采样时间。通过稳定窗口、连续反馈和时差门限约束图像—姿态关联，窗口外／陈旧数据拒绝使用。

在两秒窗口中看不到完整棋盘、覆盖率不足、模糊、边缘太近或没有时钟合格的新曝光帧，则记录 `skipped` 及原因，继续下一个路径点；遍历全部候选点后再计算。单个视觉无效点不会终止整轮。

相机 worker 退出/持续无帧、机器人未在期限内停稳、采集中移动、反馈/控制异常或 Esc/Ctrl-C 仍停止前进并保留 partial 数据，不能当作视觉跳过。收尾先停止运动；overview 持板轮次提示用户支撑并取下板，再切重力补偿，提示手动归位后退出。底层服务响应表示请求被接受，真实停止与模式切换仍需硬件验收。

## 4. 第三步：离线计算

```bash
arx5 cali --overview-right --solve
```

`--record` 遍历完成后自动求解；`--solve` 默认读取所选角色的最新完整遍历 run，并收集本站同一安装批次下其他角色的最新完整 run。采集的 `status=completed` 仅表示已遍历完毕，不表示标定 valid。缺组时输出 `status=partial` 并标明范围，不能标成全系统完成；也可用 `--runs <run-dir> ...` 明确指定输入。

计算顺序：

1. 检查全部目标点的有序遍历记录、每个跳过原因，以及有效图像/关节反馈/时钟证据。按有效观测的采集顺序均匀选出验证集，数量为 `max(5, ceil(有效数 × 20%))`，其余为训练集；划分在任何拟合之前写入 run，此后不得改动。每轮至少 15 个训练点、5 个验证点，并检查训练集的旋转多样性。
2. 用训练图像拟合各路 RGB 内参/畸变，先通过拟合 RMS ≤1 px，再用未参与内参拟合的图像检验 PnP 重投影 ≤1 px。overview 左右两轮合并训练图像拟合共享内参。任一内参 gate 失败，对应相机不进行外参拟合。
3. 用有效内参求各帧板位姿，检查正深度、平面多解与棋盘编号歧义。由训练点的实际关节角和 URDF FK 计算 wrist 安装外参、overview 相对左右 base 的外参。验证点不参与手眼拟合。
4. 检查训练及验证点的板位姿一致性（2 mm / 1°）和 PnP 重投影（1 px），逐点报告；所有 gate 通过才给出 `valid=true`。同时给出有效/跳过/训练/验证数量、内参验证报告、各点外参残差和 overview 世界系变换。

不需要分别跑两遍内参和外参采集。少于 20 个有效点、几何退化或验证失败均输出 `valid=false` 和原因。不能删除不通过的验证点来使结论变为通过；失败保留原始证据及报告，不覆盖以前的结果。验证通过表示当前数据的一致性符合阈值，独立毫米精度仍需物理参考验证。

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
    teaching/<route-id>/              # 示教原图、逐点 JSON、session.json（不随模板复用）
  runs/<role>/<run-id>/
    route.json
    run.json                         # 本站配置、HOME/首姿到位记录、遍历结果、划分和观测索引
    observations/<pose-id>.json       # 原始反馈、检测和时钟
    images/<pose-id>.png
    control-trace.json
  latest-runs/<role>.json             # 只指向完整遍历，valid 由 results 单独判定
  results/<result-id>/
    calibration.json                 # 内参、外参、残差及有效范围
    evidence-manifest.json
    evidence/<role>/                 # 原始 run 完整副本
  logs/<session-id>/
```

`--data-root`、`--poses`、`--runs` 支持明确覆盖路径。容器默认可读写 `/var/lib/arx5-collection`，读取仓库 `/workspace`；自定义路径须在容器中可见。JSON 中嵌入 URDF 与映射，离线重算不依赖原模型文件仍在原位置。

支架、底座或相机重新安装后，用 `--record --new-setup` 创建本站新批次并重新采集；不需要因此重新 teach。后续三轮不加 `--new-setup`，共享该批次。程序不跨批次拼接旧 run；每轮重新固定板都有新的 `board_mount_id`。

仓库 `config/calibration/routes/` 保存可移植的四份模板（35 / 35 / 41 / 36 个点）；部署到各站的 `calibration/routes/<role>.json` 后可直接使用默认 `--record`。原始 w5 数据在 `config/calibration/imports/w5-20260918/`（Mac）和本站历史/导入目录保留，不参与模板回放。

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
| `calibration/board.py`、`workflow.py` | 实时标记、Space 保存及退出 |
| `calibration/capture.py` | 全路径遍历、视觉跳过、本站采集证据与固定划分 |
| `calibration/orientation.py` | 用机器人旋转判别棋盘对称编号，训练/验证隔离 |
| `calibration/motion.py`、`replay.py` | 平滑限速、反馈检查、独立控制循环 |
| `calibration/hardware.py` | 专用 ROS 控制节点、硬件生命周期和相机进程管理 |
| `calibration/camera.py` | spawn 独立进程拥有 D405 pipeline；共享内存仅保留最新原生帧及配套时间戳 |
| `calibration/profiles.py`、`timing.py` | 分辨率契约，以及预览／保存／离线重算共同的时间有效性规则 |
| `calibration/geometry.py`、`solve.py` | URDF FK、两类手眼、overview 世界系、证据重算 |
| `production/lease.py` | collect／DAgger／站点初始化／标定共享的硬件锁 |
| `docker/patches/arx-x5-calibration-control.patch` | 标定专用保持、命令守卫、超时与夹爪隔离 |

标定会话独占 CAN 和相机，仅启动所选 D405 的 RealSense pipeline；不会同时启动生产相机源，也不改变生产 8/5 路 MCAP 契约。
标定全流程固定为与 collection 一致的原生 **848×480@30 RGB8**；同步 depth 使用同尺寸 Z16，保留实际工厂深度／RGB 内参、内部外参和 depth scale 元数据。不是 640×480，也不使用生产中可选的 640×360 策略缩略图。
按序列号枚举并验证 RGB/depth 支持的 profile，启动后再次核对实际尺寸、格式和 FPS，不支持时明确失败，不静默缩放或替换 profile。

流程保持三步：teach 保存位姿及 480p 检测证据，replay 采集原生 480p 快照，solve 用这些快照先拟合 480p 内参，再求外参。teach 结束时不新增单独内参计算，也不再采集 720p 或跨分辨率换算。
检测、PNG 保存和求解全用原生像素，窗口缩放仅影响显示。相机内参必须与图像 profile 一致；不匹配则在求解前拒绝。
旧 720p 路线／数据保留原始记录，当前版本不再接受其回放或求解；需要重新示教，不能只修改 JSON 尺寸冒充 480p 角点。历史 720p 结果如需复核，使用其对应软件版本。旧 848×480 路线读取时转换为可移植模板；schema 1 历史 run 需使用对应版本复核，不冒充 schema 2 新采集。当前结果仍不自动激活到生产。

先在主进程创建并实际绘制会话窗口，再打开硬件。相机进程用 `spawn` 启动，独占 SDK；
不继承 ROS/GUI 状态。图像和时间戳在同一把锁下写入一个固定大小的共享帧槽，没有无限排队。
主进程只复制最近帧，因此开窗、角点计算或主进程短暂持有 GIL 不会暂停相机获取。
取帧过程暂时没有图像时在有上限的等待内恢复，持续无帧、设备异常或子进程退出则明确报错。
时钟未稳定的图像可用于观察，不能用于采样；真实错误在归位提示前显示并记录到 camera.log。
退出及启动失败均关闭 pipeline、回收子进程和共享资源，不自动重启一个未知状态的会话。

## 8. 验收范围

软件检查包括：位姿 JSON 拒绝错误／退化数据，五次轨迹速度与加速度界限，反馈故障停止，两类手眼已知变换恢复，overview 世界变换方向，训练／验证分离，原始图像与 FK 的离线重算、证据篡改检测和硬件互斥。

初始数值门限分别为板位姿一致性 `2 mm / 1°`、图像 PnP 重投影 `1 px`，报告每点及验证集 P95／最大误差；同时单独报告固定手眼模型预测的像素误差。这是软件一致性门限，不是绝对毫米精度承诺。`absolute_accuracy_verified` 和 `cross_arm_independently_verified` 默认 false，必须有独立物理参考后才能改为已验证。

已随包保存用户提供的 X5 URDF；默认关节顺序 `joint1..joint6`、符号 +1、偏移 0，可用 `--mapping` 提供六维 `signs`、`offsets`。现场仍须核实实际反馈与这个模型的零位／轴向一致；CAD 外参不代替视觉求解，视觉手眼也不校正全部连杆和零偏误差。

现场验收还包括：控制器启动与退出、重力补偿、夹持保持、显示与相机时钟、真实速度和停止效果、棋盘可见性、整条路径以及独立毫米误差。软件测试或合成数据通过不代表这些硬件项已验收。结果尚未自动激活到生产 episode；保存和离线验证是本轮交付边界。

2026-09-18 已在两台设备离线构建 ROS/SDK 控制器与采集镜像。此前新增进程隔离、GUI 初始化先于硬件、
高低两种原生 profile（当时的验证范围，现已统一 480p）、时间异常禁止保存、进程故障回收以及两种分辨率完整像素→内参→手眼→证据重算测试。
w6 三路 1280×720 实测各 8 秒约 240 帧；主进程持有 GIL 1.5 秒期间，采集子进程仍各前进 45 帧。
没有执行机械臂运动或实际完整标定，不能据此宣称绝对毫米精度。
