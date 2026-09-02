# 配置管理

## 五类配置

`collection / dataset_pipeline / specs / runner / environment` 是唯一的运行配置分类。

- `collection` 描述一次业务采集方案：任务 ID、完整 task description、上传目录和 capture profile。DAgger 的 checkpoint、模型输入、控制、安全和采集参数也放在这里，因为它们随采集方案变化。
- `dataset_pipeline` 描述一次离线作业：数据源、输出、并发、缓存、选择范围和重组方式。
- `specs` 描述跨任务复用的契约：ROS stream、机械臂 profile、DAgger interface、π0.5/LeRobot 维度、recipe 和 JSON Schema。
- `runner` 描述如何提交或承载任务：镜像、挂载、Compose 超时、BOS 上传策略和 Viewer 参数。
- `environment` 描述一类工作站环境以及本机绑定：设备角色、相机规格、CAN、路径、触发器、监控、录制和 reset 参数。真实序列号仍写入 `/var/lib/arx5-collection/station.json`，示例位于 `config/environment/station.example.json`。

代码只保留算法、协议实现和必须一起编译的结构。任务名、Topic、频率、设备实例、路径、阈值、checkpoint、并发与输出位置不得新增为 Python 常量。Dockerfile、Python/ROS 包元数据、Skill 元数据、消息定义和补丁属于构建资产，不属于运行配置，不迁入 `config`。

源码中的 `config.py`、`configuration.py` 和 `configuration/` 只允许保存类型、解析与严格校验逻辑，不允许保存配置数据或默认方案。Episode 的 `metadata.json`、流水线的 manifest/report 与 LeRobot `meta/*.jsonl` 是运行产物格式，也不属于输入配置。

## 加载规则

默认配置根目录为运行时当前目录下的 `config`。`ARX5_CONFIG_ROOT` 可以指定另一个完整配置根目录，`ARX5_ENVIRONMENT_CONFIG` 可以指定其中一个 environment 文件。Docker Compose 会把外层配置挂载到 `/config/arx5`，修改 TOML、JSON 或 XML 后不需要重建镜像。

配置加载采用严格字段校验。未知字段、缺失字段、无效 profile、重复 stream、跨 profile 改变同一 stream 的 Topic 或类型都会直接报错。

## 常用变更

新增任务：复制 `config/collection/fold-cloth-rgbd.toml`，修改 `task_id`、`task_description`、`upload_directory`，选择已有 `capture_profile`。

新增采集方案：在 `config/specs/capture-profiles.toml` 新增 profile 和完整 stream 列表，再让 collection 文件引用新名字。Profile 名由配置动态注册，不需要修改 Python 枚举。

更换同类设备：运行 `arx5-collect station configure` 重建 station 绑定；设备路径、角色、相机规格或 CAN 参数变化时修改 environment。引入新的硬件协议或改变双臂/三相机算法拓扑仍属于新逻辑开发。

新增数据流水线任务：复制 `config/dataset_pipeline/streaming.*.toml`、`bucketlink.*.toml` 或 `composition.*.toml`。通用清洗与采样逻辑放在 `config/specs/recipes`，不要复制回 Python 包。

调整提交方式：修改 `config/runner/*.env`、上传策略或 Viewer 配置。Collection 的 task description 和上传目录是同一份事实来源，不再写入 station 配置或环境变量。

## Stage 与 Unit 编码规则

每个 stage 实际启用哪些 unit、执行顺序以及 unit 参数，只能写在 `config/specs/recipes/*.toml` 的 `stages.<stage>.units` 数组中。`config/dataset_pipeline/*.toml` 只选择 recipe，不重复声明 unit。

代码中的 registry 表示实现能力：一个 unit 类型由哪个 stage 执行、对应哪个 runner，以及接受哪些参数。它不是默认流水线，不得在代码中维护 `_REQUIRED_UNITS`、默认 unit 列表、自动插入 unit 或缺失 unit 的 fallback。已有 unit 的启用、停用、重排和参数调整必须只改 recipe；新增 unit 类型或改变 unit 的输入输出语义才需要开发代码、注册 runner 和增加参数校验。

Unit 必须通过显式产物依赖连接。Recipe 顺序缺少上游产物时直接报错，不得静默跳过、自动补齐或切换旧实现。固定的三个 stage 及其跨 Episode/Dataset 执行边界属于流水线架构；新增 stage 或把 unit 改到语义不同的 stage 属于新逻辑。

无行为变更的配置迁移必须同时满足：默认 recipe 归一化后的契约与迁移前 commit 一致；迁移前后对应的 application、worker 和 builder 测试全部通过；固定输入的语义导出 golden 与迁移前一致。语义导出必须至少比较 LeRobot create 契约、episode 与 sample 顺序、float32 state/action、解码后 RGB 像素、task、conversion report 和 source manifest。动态绝对路径归一化后再比较，不能忽略业务字段。

本次 rebase 后以迁移前最新主线 `42e61c6` 为基线。默认 recipe 契约 SHA-256 均为 `4bbb451cda1cdfcd88df448d137ee60c144b8f10794f7a2e6a9ef64f9eae6cd9`，对应的 application、worker、builder 和语义导出回归必须在两侧通过。语义测试曾发现并修复迁移时遗漏的 conversion `mixture` 与 `weighting_applied` 字段。

真实数据回归使用 BOS Episode `20260902T024111308972Z-30217f1b`，输入 MCAP 为 `2,950,048,059 bytes`、`141,072` 条消息。迁移前后完整 audit/selection 均为 `1 segment / 1,643 samples / 1,609 eligible samples`；首个 segment 的 64 个真实帧通过 pinned LeRobot `0cf8648...` 分别导出。两侧 16 个 JSON/JSONL 文件以及 1 个 Parquet 的 schema、列值和嵌入 RGB 图像字节完全一致。

真实回归在没有 ROS2 runtime 的节点使用 `mcap-ros2-support` 兼容 `rosbag2_py/rclpy` 读取边界，MCAP 消息、清洗、采样、RGB 解码和 LeRobot 序列化都是真实执行。当前 case 使用 image mode，不覆盖视频编码；视频回归应比较解码帧，不比较压缩容器字节。

## 验证

每次修改后先运行：

```bash
arx5-config validate --config-root config
```

它会检查五个顶层目录边界，并逐份加载 environment、station 示例、Vendor 控制器参数、spec、schema、recipe、collection、DAgger、dataset pipeline、Compose、env、BOS 和 Viewer 配置。仓库开发验证使用：

```bash
PYTHONPATH=src .venv/bin/pytest -q
```

数据导出语义回归可单独运行：

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/dataset_pipeline/mining_stage/dataset_generator/test_export_regression.py
```

真实 MCAP 双版本回归需要安装 `dataset`、`viewer` 和 `real-export-test` extras，并提供一个未被修改的 Episode 目录：

```bash
ARX5_REAL_EXPORT_EPISODE=/absolute/path/to/episode \
ARX5_REAL_EXPORT_WORK_DIR=/absolute/path/to/new-work-dir \
PYTHONPATH=src .venv/bin/pytest -q -s \
  tests/dataset_pipeline/test_real_export_regression.py
```

工作目录必须不存在。测试会保留两侧导出和 `comparison.json`，方便审计。
