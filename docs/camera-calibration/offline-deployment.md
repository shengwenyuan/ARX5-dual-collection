# 设备离线增量更新

入口统一为 `/usr/local/bin/arx5`，指向
`/home/lenovo/swy/ARX5-dual-collection/scripts/arx5`。
站点绑定、标定结果保存在 `/var/lib/arx5-collection`；采集数据仍在
`/home/lenovo/swy/reports`，不放进代码目录。

`docker/Dockerfile.offline` 在设备已有 `production-65bba3d` 上增量构建，
不执行 apt、下载、git fetch 或依赖解析。构建必须显式使用
`--network=none --pull=false`，基础镜像必须已在本地。

构建上下文额外提供 `offline/`（不提交二进制到 Git）：

- `controller/`：缓存的 SDK 包头文件、构建文件；控制器 CPP/HPP 使用
  `c78328785ea23a81d908e1dcc551eca992f2f1e9` 对应源码及本仓库的
  go-home、policy-control-latch、calibration-control 三个补丁。
  编译时强制链接基础镜像中的 `libarx_x5_src.so`，只更新控制器包装程序和 launch。
- `python/`：设备现存 OpenCV GUI 包 `cv2`、`opencv_python.libs`、dist-info。
  本次离线组合为 OpenCV **4.11.0.86 / Qt5** 和基础镜像 NumPy **1.26.4**。
  常规联网构建的 calibration extra 为 4.12；这次特意复用已存在的 4.11，
  必须在该组合上运行标定测试，不可用 headless 包替代窗口版。
- `lib/`：基础镜像缺少的 `libGL.so.1`，来自同机 Ubuntu 24.04 已安装文件。
- `manifest.json`：来源、版本和以上文件 SHA-256 清单，随镜像保存到
  `/opt/arx5-runtime/offline-manifest.json`。

```bash
docker image inspect arx5-dual-collection:production-65bba3d >/dev/null
docker build --network=none --pull=false -f docker/Dockerfile.offline \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  -t arx5-dual-collection:calibration-candidate .
```

先验证 Python/ROS 导入、控制器链接、标定数值测试和 OpenCV 窗口；不连接 CAN，
不以试运行机械臂验证镜像。通过后，production、dagger、calibration 三个功能标签
指向同一个新镜像，沿用已有 policy-server 镜像。最后切换唯一入口，删除旧 release
代码目录、rtc 软链接和旧 collection 镜像标签；保留数据、站点配置和其他项目镜像。

## 开始位姿示教

在设备的图形桌面终端运行下列对应命令。普通 SSH 没有显示窗口；远程操作需已配置
X11 转发或进入设备桌面。四条路线各自采集，不在一条命令中混用：

```bash
arx5 cali --left-wrist --teach
arx5 cali --right-wrist --teach
arx5 cali --overview-left --teach
arx5 cali --overview-right --teach
```

wrist 路线：棋盘固定在环境里，拖动对应机械臂改变相机视角。
overview 路线：棋盘刚性固定在对应末端，拖动该臂让 overview 看到不同板姿态。
另一只机械臂保持不动。启动进入重力补偿，不自动 HOME。

首次输入内角点列、行、单格 mm；画面检测有效且停稳后按 **Space** 保存实测关节角。
确认屏幕 A 编号对应物理标记，反向按 R。建议每路 25 个独立姿态，覆盖不同方向、
倾角和距离；Enter 验证并完成，Esc 留草稿，退出按提示手动归位。

默认文件为 `/var/lib/arx5-collection/calibration/routes/<role>.json`。
每次 Space 原子落盘；Enter 后才允许自动回放。再次 `--teach` 是新示教，
旧文件进入 `routes/history`。后续依次用同一路线的 `--record`、`--solve`，
自动回放默认速度上限 0.10 rad/s，且先要求人工放到首个记录姿态。

## 2026-09-18 部署记录

两机运行代码版本 `fc3b01a`。各机 production、dagger、calibration 三个标签共用
同一个镜像；底层既有镜像不同，因此两机最终镜像 ID 不同：

| 设备 | 镜像 ID 前缀 | 删除旧 release 目录 | 删除旧 collection 镜像标签 |
| --- | --- | --- | --- |
| w5-arx5 | d66058474628 | 8 | 18 |
| w6-arx5 | 21c4e6409198 | 8 | 20 |

两机各通过 165 项 calibration/production/DAgger 测试，实际桌面 OpenCV 开窗、
站点配置解析、URDF 加载、ROS/RealSense 导入及控制器动态链接检查通过。
未启动 CAN 控制器或执行机械臂运动。全仓库测试在本地此前为 327 passed / 1 skipped；
设备运行镜像未额外安装全套开发测试依赖。

旧 rtc 软链接已删除，w5 三个已停止的 MIX_WORK 服务中的仓库路径已改成唯一目录，
仅 daemon-reload，没有启动服务。站点配置 SHA-256 前后相同，reports 数据不变。
旧 config/plans 中的配置和笔记作为非执行资料保留。

每台机的部署证据、旧标签/目录清单和离线输入归档在：
`/var/lib/arx5-collection/deployments/20260918-fc3b01a/`。
后续离线更新可在独立构建临时目录解压其中的 `offline-inputs.tgz`，并显式传
`--build-arg BASE_IMAGE=arx5-dual-collection:production`；首次使用的
`production-65bba3d` 旧标签已清理，不再依赖其标签存在。
