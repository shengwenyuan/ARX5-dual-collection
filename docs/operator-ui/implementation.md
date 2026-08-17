# Operator UI 实施计划

- Status: `alignment-round-2-beta1-visual-prototype`
- Branch: `codex/operator-ui`
- Parent: `meta_plan.md`
- Scope: 本地轻量网页界面，不改变采集与落盘契约

## 定调

- 使用 JavaScript/TypeScript 开发，在采集工作站本地启动，通过浏览器访问。
- 桌面端优先，采用简单、克制的工业工具风格；首版不引入重型组件库或素材系统。
- UI 只做操作、状态展示和进程编排，不复制 Station、Episode、Recorder 或 Store 业务逻辑；后续按钮只调用 CLI 已有功能入口。
- 浏览器不直接执行命令。一个本地 JS Bridge 使用显式 argv 启动受支持的 `arx5-collect` CLI，并向前端提供结构化状态与事件。
- 原始 Episode 仍严格由 Python 核心提交 `episode.mcap + metadata.json`；UI 不读写 MCAP，不伪造采集状态。
- beta1 只实现可独立启动的视觉原型和假状态交互，不连接 CLI、ROS、设备、Station 配置、报告目录或 Docker Socket。

## beta1 冻结边界

- 使用 React + TypeScript + Vite，保持单一前端应用，不提前建立多包 monorepo。
- 使用独立、无特权 Docker 容器在 Ubuntu 上运行，只绑定 `127.0.0.1` 的 UI 端口。
- 不使用 `privileged`、`host network`、`/run/udev`、`/var/lib/arx5-collection` 或 `/reports` 挂载，不影响当前设备和采集容器。
- 所有按钮、任务、设备、Episode 和相机画面均来自显式 Mock Adapter；组件不直接 import 假数据。
- beta1 的目标是反复截图检查布局、状态、窗口和按钮逻辑，不验证真实采集功能。
- beta1 通过后再对齐真实 Bridge；更多功能不在当前阶段扩展。

## 页面布局

```text
┌──────────────────────┬─────────────────────────────────────────┐
│ TODO Tasks 约 70%    │ 当前任务描述/提示             状态+计时 │
│ 首版打桩、可滚动     ├─────────────────────────────────────────┤
│                      │                                         │
│                      │ 三路实时相机预览                         │
├──────────────────────┤ left / overview / right                 │
│ 已落盘 Episodes 30%  │                                         │
│ 可滚动               ├─────────────────────────────────────────┤
│                      │ 核心操作区                               │
└──────────────────────┴─────────────────────────────────────────┘
```

- 左栏固定约 30% 页面宽度；上部 TODO Tasks 占左栏约 70%，下部 Episode 列表占约 30%。
- 右侧上方保留任务说明、提示和错误文本；右上固定显示权威状态与录制秒表。
- 右侧中部将 left、overview、right 三路 RGB 预览等宽、并列、尽量放大；真实版本的空间角色必须来自 Station 配置。
- 右侧下部放置少量高频操作，危险操作与主按钮保持明显层级。
- 首版固定面向 Ubuntu 工作站的 1920×1080 Chromium 横屏，不承担手机或复杂响应式布局。

右侧操作区按频率和风险分为三组：

```text
设备与准备                 Episode 主操作                    系统与辅助
Calibration（占位）        开始/完成本条（最大主按钮）        磁盘容量
Station 初始化             中止本条（危险次按钮）              日志
设备检查                   启动/退出 Session                  数据检查（占位）
```

- `开始/完成本条` 必须是视觉焦点，但只有 Session READY/RECORDING 时可用。
- `中止本条` 与 `退出 Session` 不紧邻主按钮，防止误触；退出只在 READY 可用。
- 磁盘剩余空间和预计可录制分钟数常驻显示，不做成需要点击才能发现的信息。

## 操作与占位

首版核心操作：

1. `Calibration`：禁用或明确标记“未实现”，只保留布局与未来入口。
2. `Station 初始化`：未来启动已实现的 `arx5-collect station configure`，使用网页内终端窗口展示原始交互、结果与失败原因。
3. `开始/结束录制`：复用 `arx5-collect run` 的生产 Session 和 activate 语义，不建立第二套 Recorder。
4. `中止本条`：建议作为独立次级按钮，映射 abort 语义并要求明确视觉反馈。
5. `设备检查`：建议复用 `arx5-collect devices`，以七设备列表展示 matched/failed。
6. `退出 Session`：建议仅在 READY 可用，触发统一顺序回收，不等同于杀容器。
7. `日志/错误详情`：首版可用折叠面板占位，避免把原始日志铺满主页面。

TODO Tasks、Calibration、任务下发和人工 `fail` 标注首版只打桩，不伪装为可用功能。

beta1 中上述按钮只操作 Mock 状态：Station 初始化打开假终端窗口，Calibration 与数据检查打开明确的占位窗口，设备检查返回固定七设备示例，Session/Episode 按钮驱动可复现的假状态机。

## 权威状态

前端至少展示：

```text
OFFLINE
STARTING
READY
HOMING
RECORDING
FINALIZING
ABORTED
ERROR
SHUTTING_DOWN
```

- 正式版状态、开始时间和 Episode ID 必须来自采集后端事件，不能由按钮点击结果自行推断；beta1 明确显示 `SIMULATION` 标识。
- 秒表只在 `RECORDING` 运行，以后端开始时间计算；刷新页面后必须能恢复。
- 按钮根据状态机启停，禁止重复点击启动两个 Session 或两个 Recorder。
- CLI 非零退出、设备失配、相机掉流和落盘失败必须显示可操作错误，不静默回 READY。

## 本地架构候选

beta1 使用纯前端 TypeScript；后续真实版本增加本地 JS Bridge：

```text
Browser UI
  ├─ layout / camera preview / controls
  └─ HTTP + WebSocket

Local JS Bridge
  ├─ CLI Process Supervisor
  ├─ authoritative state adapter
  ├─ Episode directory reader
  └─ camera preview bridge

arx5-collect / ROS 2 / Station / Episode Store
```

- Bridge 只允许调用白名单命令，不接受浏览器传入任意 Shell 字符串。
- 所有子进程使用显式 argv 和受控生命周期，不使用 `shell=True` 风格拼接。
- UI 服务只监听 `127.0.0.1`，首版不做局域网访问与身份认证。
- 生产 Session 仍以 `arx5-collect` 作为 collector 容器主进程；UI 不把采集逻辑搬进 Node。
- stdout 文本解析不能成为正式状态协议。Bridge 可以用 PTY 原样承载 `station configure`，但生产按钮、计时和刷新恢复仍需要稳定的结构化事件。

现有 Trigger 事实：`AutoTriggerFactory` 在已配置踏板可用时只返回 `PedalTrigger`，仅在踏板缺失时回退 `KeyboardTrigger`。因此向 PTY 写入 `SPACE/A` 不能与真实踏板并行。beta2 若保留网页按钮与踏板同时有效，应在 CLI 内组合统一 `RecordTrigger`，由 UI 调用同一 activate/abort 语义；这不是 beta1 范围。

## 相机预览边界

- 预览只用于操作者构图检查，不进入 Episode，不改变 1280×720@30 原始录制链路。
- 页面应使用低带宽派生预览，避免把六路原始 RGB-D 传给浏览器；只显示三路低分辨率、低帧率 RGB，三块等宽并列。
- 预览断开必须与录制数据流故障分开表达，不能仅凭网页画面冻结判定 MCAP 缺帧。
- 首版不显示 Depth、点云、CameraInfo 或标定叠加层。
- beta1 使用本地 CSS 测试卡和角色标识模拟画面，不下载视频、不打开相机。

## 视觉方向

- 使用系统字体、CSS Grid/Flex、8 px 间距节奏、少量中性色和一个主操作色，不依赖图片素材也能完成。
- 参考轻量控制台、录音工具和工业 HMI 的信息层级，但不直接复制第三方受版权保护的素材。
- 状态颜色只作辅助，始终同时显示文字；录制使用高辨识度红色，异常与 abort 不共用正常结束样式。
- 采用全宽高密度工具布局、固定左栏、清晰 key lines 和等比例视频卡；参考设计系统只提取栅格与可访问性原则，最终使用原创 CSS token。

建议基调为深灰蓝工作台、低对比边框、米白正文；READY 使用绿色文字和轮廓，RECORDING 使用红色实心状态，WARNING/ABORT 使用琥珀色，ERROR 使用红色文字加图标。状态始终同时使用文字、形状和颜色表达。

## beta1 假数据与窗口

- 三条静态 TODO Task 可选中，并只更新顶部任务标题与描述。
- Episode 列表最新优先，展示时间、时长、outcome、大小和 warning；展开显示 ID 与路径，不提供删除、编辑或回放。
- 内建隐藏式 Demo Controls，可切换全部状态、相机在线/离线、磁盘不足和七设备检查结果，便于截图验证。
- Station 初始化使用网页内终端窗口；日志使用右侧抽屉；Calibration 和数据检查使用轻量模态窗口。
- 刷新页面重置为固定 Mock 初态，不读浏览器外部状态。

## 迭代单位

1. 冻结 beta1 wireframe、视觉 token、Mock Contract 与窗口行为。
2. 建立 React/TypeScript/Vite 工程和独立无特权 Docker 启动入口。
3. 实现静态布局、三路假预览、任务/Episode 列表、操作区和全部窗口。
4. 实现可复现 Mock 状态机、计时器、按钮禁用规则与 Demo Controls。
5. 在本地浏览器和 W3 Ubuntu Docker 中反复截图，完成 1920×1080 视觉验收。
6. beta1 结论回写后，再对齐 CLI Bridge、Trigger 组合和真实预览协议。
7. beta2 才接入 `devices`、Station CLI、生产 Session 与只读 Episode 列表。
8. W3 真链路通过后再部署 W4，完成 success、abort、刷新恢复和统一退出验收。

## 已对齐

- UI 与 CLI 尽量解耦，功能按钮复用 CLI 入口，不重复实现业务逻辑。
- 点击独立按钮启动 Session；Episode 使用单独的开始/完成、中止按钮；退出 Session 只在 READY 可用。
- Station 初始化使用网页内终端承载现有 CLI。
- 未来网页按钮与实体踏板并行有效，由统一触发语义处理。
- 三路预览只显示低分辨率、低帧率 RGB，等宽并列且不能争抢录制资源。
- Episode 列表最新优先，只读展示时间、时长、结果、大小、warning、ID 和路径。
- Ubuntu Docker、1920×1080 Chromium、本机访问是首版固定环境。
- TODO Tasks 使用可选择的静态假任务，只更新 UI 当前任务描述。
- 设备检查、中止、退出、磁盘信息、日志和数据检查占位均进入布局。
- beta1 完成前不扩展更多功能。

## 仍待对齐

- beta1 是默认深色工作台，还是需要同时提供浅色版本供选择。
- 三块相机卡严格等高，还是允许 overview 略宽但保持同一行。
- Session 启动与 Station 初始化是否使用同一个底部区域，还是 Station 初始化放到顶部设置入口。
- Demo Controls 在 beta1 页面常驻、抽屉隐藏，还是只通过 `?demo=1` 显示。
- beta1 是否要求浏览器自动进入全屏/Kiosk；默认建议普通 Chromium 窗口，便于调试和截图。

## 参考依据

- Carbon 2x Grid：采用 8 px 基础间距、固定侧栏、全宽高密度界面、对齐 key lines 和固定比例图像卡。
- Carbon Status Indicators：状态不能只依赖颜色，列表中的状态图标与文字保持对齐。
- xterm.js：未来 Station CLI 终端窗口只提供终端表现层，不复制 CLI 交互业务。
- Vite：使用官方 React + TypeScript 模板作为 beta1 最小工程基线。
