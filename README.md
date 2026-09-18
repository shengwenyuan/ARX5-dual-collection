# ARX5 双臂采集

本仓库负责双臂、三相机的数据采集与本地落盘，以及在线 DAgger。每条正式 Episode 交付 `episode.mcap` 和 `metadata.json`。

## 功能边界

- 普通示教与在线 DAgger，设备识别、站点配置、触发器、复位和流健康检查。
- RGB-D：三路 RGB、三路对齐深度、双臂状态；RGB-only：三路 RGB、双臂状态。
- 停止录制后默认执行 Zstd 无损压缩、MCAP 审计和原子提交；进入 READY 后才开始下一条。
- 离线清洗、训练样本筛选、LeRobot 转换/重组、数据集 Viewer 和上传由外部系统负责，本仓库不提供这些入口。

## 采集入口

在已安装 Docker、完成镜像构建和站点初始化的 Linux 采集机上，从仓库目录运行：

```bash
export ARX5_OUTPUT_ROOT=/home/lenovo/swy/reports/2026-09-18/example-task
export ARX5_TASK_DESCRIPTION='Describe the task'
scripts/arx5 collect --rgb-only
```

不带 `--rgb-only` 时录制 RGB-D。输出路径按现场磁盘挂载配置选择；上述目录仅为示例。

在线 DAgger 使用 `scripts/arx5 dagger --mode shadow` 或 `--mode takeover`，可附加 `--rgb-only`。运行前需配置站点的 `dagger.env`、策略配置及 checkpoint；详见 [DAgger 实施说明](docs/dagger/implementation.md)。仓库内的四份 `config/dagger.*.toml` 是示例/历史运行配置，不代表当前设备已部署对应模型。

底层 `arx5-collect` 提供设备检查、站点初始化、普通采集和 DAgger 入口。`run` 与 DAgger 的 `--no-compress` 参数可关闭结束后的压缩；主机简化脚本不转发该参数。

## 站点配置

持久化站点文件为 `/var/lib/arx5-collection/station.json`，示例见 [station.example.json](config/station.example.json)。新配置使用 schema v5，只包含设备身份和 ROS Domain ID；任务描述由采集入口显式指定。

已有 v3/v4 配置可直接读取；v4 旧路由字段不参与运行。读取不修改站点文件，下次通过配置写入入口保存时升级为 v5。不得用示例文件覆盖真实设备绑定。详见 [站点初始化](docs/station-initialization/implementation.md)。

## 开发验证

纯 Python 回归无需连接机器人：

```bash
python -m pip install -e '.[test]'
PYTHONPATH=src python -m pytest -q tests
```

ROS、SDK、相机及真实 MCAP 链路仍需在对应 Linux 容器中验收。代码修改不会自动更新已部署镜像。

完整边界和本次迁移说明见 [采集仓库边界](docs/architecture/collection-scope.md)。
