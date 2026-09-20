# w5-arx5 infer 镜像部署记录

2026-09-20。按用户追加要求，先提交开发版本，再离线更新镜像；真机验收与上层 gitlink 锁定仍未执行。

- collection 实现提交：`0260774e8f04815f2b1a2d85ed644edb61cded1b`。
- 镜像构建源码提交：`064f8d9a1fe9c4e3b3fb6c485c9fd79a8ded85ae`，新增 `docker/Dockerfile.infer-update`。
- 已部署镜像：`sha256:ed5e7447389de7b383aab0c076507a292062e68dbe01803c5959dc62ec90db8c`。
- `arx5-dual-collection:production`、`:dagger`、`:calibration`、`:infer` 指向同一新版镜像。

## 更新与验证

复用设备现存 `1ace56d` 镜像及全部依赖。比对 SHA 后仅传输差异文件，压缩包 96,101 字节。构建使用 `--network=none --pull=false`，仅重新生成本仓库 ROS 接口包并安装应用代码；pip 使用 `--no-index --no-build-isolation --no-deps`。未下载或重建 SDK、OpenPI 依赖和模型，policy-server 镜像保持 `d608fd9c14d4`。

在隔离容器内验证：

- ROS `InferCommand` 生成、导入，以及 infer CLI 通过。
- 全量测试 **395 passed**；一条既有 OpenPI 弃用警告。测试所需 JSON schema 依赖临时复用设备已安装包，不写入运行镜像。
- 真实 rosbag2/MCAP 的合成 success、fail 两局均通过：每局 4 条命令，步号 0–3，14D 动作一致；分别录到 18、17 条模拟传感器消息，最后传感器时间晚于最后动作。普通 fail 正常提交，errors 为空。
- 烟测曾因测试脚本先建 ROS 节点、后加载 station 改写 domain 而超时；调整为与正式入口一致的初始化顺序后，通过原样的生产 Recorder。未因此修改生产逻辑或 DDS 配置。

## 路径与清理

入口保持 `/usr/local/bin/arx5` → `/home/lenovo/swy/ARX5-dual-collection/scripts/arx5`。
采集输出仍为 `/home/lenovo/swy/reports`；站点、policy、标定目录内 18 份配置 SHA 前后相同。

已删除被替代的 collection 镜像 `14e6d8bf10c5`、`272dc21dc70d` 及临时候选/base 标签。新版所依赖的共享层保留；其他项目镜像、policy-server、checkpoint、已有采集数据不属于删除范围。

设备证据目录：`/var/lib/arx5-collection/deployments/20260920-064f8d9/`，包含构建日志、测试日志、合成 MCAP、烟测脚本和部署 manifest。

## 真机前仍待处理

未启动机械臂、相机、CAN 或模型服务。踏板释放/长按与实际多局行为仍待用户配合验收。

本站原先不存在 `/var/lib/arx5-collection/dagger.env`；旧 `dagger-policy.toml` 中 `policy_wait_timeout_s=0.5` 加默认 margin 0.05 秒，超过当前 `max_delay_steps/control_rate_hz=10/25=0.4` 秒的 RTC deadline，因此未通过现有配置校验。该问题在本轮改动前的配置/代码组合中也存在。本轮保留原配置，真机启动前需补齐路径映射并结合实际推理延迟确定合规超时，不能直接宣称已可开始实机 infer。
