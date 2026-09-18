# 采集仓库边界

更新：2026-09-18。基线：`20ad014`。

## 数据交付

本仓库以本地完整提交 `episode.mcap + metadata.json` 为终点。保留 RGB-only / RGB-D、在线 DAgger、设备与站点管理、复位、流健康检查、Zstd 压缩、MCAP 审计和原子提交。压缩后的消息与采集结果校验仍属于落盘完整性职责。

上传、离线清洗、训练样本筛选、数据集转换、数据集重组与数据集 Viewer 已移除。外部系统只读已提交的 Episode，自行负责传输和后处理。原始 Topic、消息、控制权事件及 Episode metadata 契约保持不变。

## 移除范围

- `src/arx5_collection/cleaning/`、`pi05_dataset/`、`dagger_dataset/`、`streaming_conversion/`、`lerobot_recomposition/`。
- `dataset_cli.py`、`artifacts.py`、`atomic.py`、`viewer.py`、`viewer_static/`。
- `bos_upload_runtime.py`、`tools/bos_upload_episodes.py` 和主机脚本的 `upload` 子命令。
- 对应的测试、转换/上传配置、10 个离线产物 Schema、数据处理脚本及文档。
- `docker/Dockerfile.dataset`、`arx5-dataset` / `arx5-viewer` 入口以及 dataset / dataset-v3 / viewer 可选依赖。

保留 `episode-metadata-v1.json`。在线 `dagger/`、`gripper.py`、相机 Snapshot 和策略推理仍由采集侧维护；模型所需的输入格式、训练所得 checkpoint 和推理参数不属于离线清洗实现。

## 配置兼容

- 新初始化写入 station schema v5；任务描述与输出目录由采集入口提供，不再校验上传任务路由。
- 已部署的 v4 文件仍可读取。`task_upload_routes` 仅作为旧格式字段接受，不保存到运行时模型，也不执行路由校验。
- v3/v4 配置读取不会改写文件；下次保存升级为 v5，移除旧字段，保持设备绑定和 ROS Domain ID（除用户显式修改的值）不变。
- v1/v2 仍可读取，但缺少 ROS Domain ID 时不能启动生产；v2 可通过 `station set-ros-domain-id` 升级，v1 需重新初始化。
- schema v5 严格校验字段，不允许继续加入旧上传路由字段。

仓库原有未跟踪的训练配置、标定文档及本地产物不在本次删除范围。未访问或修改设备上的配置、容器、数据及上传凭据。

## 在线配置补全

基线提交本身遗漏了在线 DAgger 测试依赖的四份配置。本次从已有部署分支 `codex/temp-rgb-only-recording` 恢复以下原文件，以便保留在线模式并完成回归：

- `config/dagger.policy.example.toml`
- `config/dagger.pi05-stacking-v2.toml`
- `config/dagger.pi05-stacking-v3-rtc.toml`
- `config/dagger.pi05-fold-cloth-20260828-train-rtc.toml`

这些文件用于在线策略加载与控制参数，不启动训练、清洗或上传，也不会自动改变生产设备使用的策略。

## 验证

- `PYTHONPATH=src .venv/bin/python -m pytest -q tests`：300 passed，1 skipped。跳过项依赖本机未安装的 openpi-client transport。
- 回归覆盖普通采集与 RGB-only、Episode 生命周期、压缩与异常提交、站点 v4 读取/v5 原子迁移、在线 DAgger 的配置与控制逻辑。
- 已从干净源码目录构建 wheel：包含 71 个 Python 文件，唯一安装命令为 `arx5-collect`；未包含已移除模块、Viewer 静态资源及 dataset / dataset-v3 / viewer 可选依赖。主机入口帮助只列出 `collect`、`dagger`。
- 未执行实机采集或生产部署；Linux ROS/SDK、GPU 推理及真实硬件链路仍需在部署时验收。

未来部署需重新构建 Collector 镜像。旧镜像仍含原有代码，不能仅靠更新工作区认定设备已切换新版本。旧版本软件不认识 v5 配置，回滚前应保留各站点原配置备份。
