# Station 镜像发布与一键部署计划

- Status: `future-requirement-not-implemented`
- Parent: `docs/station-initialization/implementation.md`
- Scope end: 工作站进入设备编号对齐前

## 目标

未来不再把源代码传到采集工作站，也不在工作站本机构建 production 镜像。发布流程先产出经过测试的 Docker 镜像和轻量部署包，工作站只执行一个入口：

```text
deploy_station.sh
  -> 主机预检
  -> 拉取已发布 production 镜像
  -> 初始化本地持久化目录
  -> 容器运行时自检
  -> 只读设备盘点
  -> READY_FOR_STATION_CONFIGURE
```

脚本到此停止。双臂、D405 和踏板的编号与角色绑定仍由 `arx5-collect station configure` 完成。

## 支持的主机基线

- 正式工作站固定使用 Docker 官方仓库发布的标准 Docker Engine 与 Compose plugin。
- 不支持 Snap Docker。Snap confinement 不能稳定访问 `/var/lib/arx5-collection` 等正式主机路径，也不适合作为 privileged、CAN、USB 和 udev 生产部署基线。
- Docker Engine 安装和用户组授权属于工作站一次性 provisioning，不随每个应用版本重复执行。
- `/var/lib/arx5-collection` 是正式宿主持久化路径；不因容器运行时限制回退到用户 HOME，也不建立 W3/W4 特例。
- 主机 provisioning 完成后必须验证 Docker 用户权限、任意固定路径 bind mount、privileged、host network 和 `/run/udev` 访问，再标记工作站可部署。

## 发布物

一次正式发布至少包含：

```text
registry/<project>/arx5-dual-collection:<version>
station-deploy-<version>/
  deploy_station.sh
  compose.yaml
  release.env
  README.md
```

- production 镜像包含 ARX5 SDK、librealsense、ROS2 工作区、Python 运行时和固定任务契约。
- 部署包只包含部署所需文件，不包含源代码、真实设备编号、station.json 或报告。
- `release.env` 固定镜像版本、部署 schema 和最低运行条件。
- 稳定版本优先；默认记录版本和镜像标签，不强制 SHA。只有发布渠道或计划明确要求时再增加 digest 对齐。
- 镜像仓库、命名空间和认证方式在实现前单独对齐。

## 单一部署入口

预期使用方式：

```bash
./deploy_station.sh
```

首版脚本保持幂等、非交互业务逻辑和明确失败：

1. 检查 x86_64 Linux、Docker Engine/Compose、当前用户 Docker 权限、磁盘和内存。
   检测到 Snap Docker 时直接拒绝并指向一次性主机 provisioning 文档。
2. 检查 `privileged container + host network` 能力，以及 `/run/udev` 是否存在。
3. 检查目标镜像标签，拉取已发布镜像；不在工作站执行 Docker build。
4. 初始化 `/var/lib/arx5-collection/` 和报告目录，但绝不创建空白或占位 station.json。
5. 若 station.json 已存在则保持字节不变并明确报告，不自动覆盖或迁移。
6. 运行镜像内建 smoke/self-test，确认 ROS2、ARX5 SDK、librealsense、MCAP 和 CLI 可用。
7. 执行 `arx5-collect station inspect --json`，只读盘点 USB2CAN、D405 和 hidraw 踏板。
8. 确认没有启动 CAN、Vendor ROS 进程、相机流或 Episode Session。
9. 输出 `READY_FOR_STATION_CONFIGURE` 和下一条人工命令后退出零状态。

部署脚本可以请求 sudo 创建固定主机目录，但不负责安装 Docker、修改用户组、修改 Docker socket 权限或放宽设备权限。这些一次性主机前置条件由独立 provisioning 文档维护；部署脚本只检查并给出明确修复提示。

## 应用与脚本边界

部署脚本只编排主机和容器，不包含硬件身份判断：

```text
deploy_station.sh
  host preflight
  image pull
  directory bootstrap
  container smoke
  station inspect

arx5-collect station configure
  显式 ROS Domain ID
  USB2CAN 左右臂识别
  D405 空间角色绑定与真实流验证
  双踏板语义绑定
  station.json 原子提交
```

- `station inspect` 和 `station configure` 必须复用同一 Inventory 组件。
- Shell 不解析 udev、RealSense 或 hidraw 信号，不拼接 ROS/CAN 子进程。
- 未来 UI 调用应用服务，不调用部署脚本完成编号绑定。

## 失败与重复执行

- 镜像拉取、目录权限、自检或盘点失败时返回非零，不进入编号对齐。
- 脚本中断后不留下半写 station.json，也不运行后台容器。
- 重复执行不得覆盖 station.json、报告或用户数据。
- 已拉取正确镜像时允许跳过下载，但仍执行环境与容器 smoke。
- 检测到错误架构、Docker 无权限、USB2 设备或设备数量异常时明确报告；设备异常允许部署完成，但不得输出 READY。

## 测试与验收

- 发布流水线在发布镜像前运行完整单元、容器和 SDK 构建测试。
- 工作站只运行镜像内建 smoke，不依赖仓库源码或挂载测试目录。
- 在一台无源码、无 station.json、无历史镜像的同规格工作站验证一键部署。
- 部署结束时工作站只有已发布镜像、部署包、空的 Station 持久化目录和必要报告目录。
- 用户随后单独执行 `arx5-collect station configure`，可完成从零设备编号对齐。

## 实现前待对齐

- 镜像仓库及认证方式。
- 发布版本命名和稳定渠道策略。
- 部署包的分发位置与更新策略。
- 镜像内建 `self-test` 与 `station inspect --json` 的最终输出 schema。
- Docker Engine 与 Ubuntu LTS 的支持矩阵及主机 provisioning 版本。

## W4 路线验证

- 2026-08-17，W4 使用 Docker 官方仓库的标准 Docker Engine 完成 production 镜像加载、固定 `/var/lib/arx5-collection` 读写挂载、容器测试和只读设备盘点。
- 随后通过唯一应用入口完成 Station 编号与角色绑定并原子提交配置，证明“正式镜像部署到编号初始化前，硬件身份由应用配置”的边界可行。
- 本轮仍是人工执行的路线验证；`deploy_station.sh`、镜像仓库发布和无源码部署包尚未实现，计划状态保持 future requirement。
