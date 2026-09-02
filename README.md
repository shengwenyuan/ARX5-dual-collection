# ARX5 Dual Collection

ARX5 双臂 Episode 采集、数据挖掘和 LeRobot Dataset 生成工具。运行时配置统一放在仓库外层 `config/`，分为五类：

| 目录 | 管理内容 |
| --- | --- |
| `config/collection` | 任务、prompt、采集 profile、上传目录和 DAgger 策略 |
| `config/dataset_pipeline` | 数据发现、清洗、挖掘、生成和重组作业 |
| `config/specs` | stream、机械臂、DAgger、π0.5 契约，JSON Schema 和 recipe |
| `config/runner` | Compose/主机提交参数、上传策略和 Viewer 参数 |
| `config/environment` | 工作站设备角色、路径、相机、CAN、触发器和运行时阈值 |

默认配置根目录是当前目录下的 `config`。从其他目录运行时设置 `ARX5_CONFIG_ROOT`；切换 environment 文件时设置 `ARX5_ENVIRONMENT_CONFIG`。

Compose 文件统一位于 `config/runner`，Vendor 控制器参数位于 `config/environment`。`src` 和 `docker` 不保存 recipe、schema、policy、设备参数或运行编排副本。

源码中名称包含 `config` 或 `configuration` 的 Python 模块仅负责解析和校验；配置值仍以外层 `config/` 为唯一事实来源。

新增普通采集任务时复制一份 `config/collection/*.toml`，修改任务字段并直接运行：

```bash
arx5-config validate --config-root config
ARX5_OUTPUT_ROOT=/absolute/reports/2026-09-02/session \
  scripts/arx5 collect --collection-config config/collection/fold-cloth-rgbd.toml
```

数据挖掘入口：

```bash
arx5-dataset build \
  --config config/dataset_pipeline/streaming.fold-cloth-2026-08-28-full-v1.toml
```

使用 PFS BucketLink 导入一个 BOS 批次后执行同一流水线：

```bash
arx5-dataset bucketlink-to-lerobot \
  --config config/dataset_pipeline/bucketlink.organize-screwdriver-bits-2026-09-01-v1.toml \
  --output /absolute/lerobot/path \
  --run-id organize-screwdriver-bits-2026-09-01
```

Stage 的 unit 选择、顺序和参数统一由 `config/specs/recipes/*.toml` 管理；源码 registry 只注册可执行能力，不保存必选 unit 清单或默认流水线。

数据流水线迁移还由语义导出回归测试保护。它使用固定 selection 和图像输入，逐项校验传给 LeRobot 的 features、episode、float32 state/action、RGB 像素以及 conversion/source manifest；基线为迁移前最新主线 commit `42e61c6`：

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/dataset_pipeline/mining_stage/dataset_generator/test_export_regression.py
```

外部提供真实 Episode 后，真实回归测试会从同一个 MCAP 分别运行 `42e61c6` 和当前工作树，完整比较 audit/selection，并比较真实 LeRobot image-mode Parquet 的 schema、列值和嵌入图像字节：

```bash
ARX5_REAL_EXPORT_EPISODE=/absolute/path/to/episode \
ARX5_REAL_EXPORT_WORK_DIR=/absolute/path/to/new-work-dir \
PYTHONPATH=src .venv/bin/pytest -q -s \
  tests/dataset_pipeline/test_real_export_regression.py
```

目录边界、字段归属和新增方案流程见 [配置管理文档](docs/configuration-management.md)。
