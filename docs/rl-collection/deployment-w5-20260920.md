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

## 配置补齐与历史问题归属

后续核实：w5 当时确实没有 `/var/lib/arx5-collection/dagger.env`。此前 timeout 校验失败明确对应 **2026-08-28 17:38 的旧 `/var/lib/arx5-collection/dagger-policy.toml`**，SHA 为 `10bdb3dd54b430ccb0ae7b3efc613e748de3ef07d8c7cf49e815578b0f901147`，与 w3 的 `dagger-policy.toml.pre-e49bb09` 备份逐字节一致。它的 0.5 秒 timeout + 0.05 秒 margin 超过 10/25=0.4 秒预算。这是历史配置与现有校验规则不匹配，**不是当前新版 RTC 仍有故障或推理实际超时的结论**。

2026-09-20 只读参考 w3 当前 `dagger.env` 和 policy TOML，已在 w5 补齐：

- `/var/lib/arx5-collection/dagger.env`：使用 w5 本机源码、checkpoint 和 reports 路径，policy 配置指向新 `infer-policy.toml`。旧 `dagger-policy.toml` 原样保留，不再是默认生效文件。
- `/var/lib/arx5-collection/infer-policy.toml`：沿用 w5 checkpoint、25 Hz、动作和夹爪配置；参考 w3 将 request timeout 设为 200 ms、policy wait 设为 280 ms，显式 margin 为 50 ms。满足 `200 < 280` 且 `280 + 50 <= 400 ms`，实际延迟仍在真机阶段验收。
- 复用现有两侧镜像，通过 collector/server 配置解析、checkpoint 完整树 SHA、RTC 类型与归一化资产检查；RGB-only 和完整采集的 Compose 配置、只读模型挂载、输出映射与宿主机入口 dry check 均通过。

本次只新增外部挂载配置，没有重建镜像或下载组件，没有修改 w3、站点标定、旧配置、模型和已有采集数据。补齐记录在 `/var/lib/arx5-collection/deployments/20260920-infer-config/`。上节“18 份配置 SHA 不变”描述前次镜像更新时的检查；本次保留其中旧文件，仅新增上述两个配置。

未启动模型服务、机械臂、相机或 CAN。真机命令、模型替换方法和踏板验收步骤见 [w5 infer 验收入口](w5-infer-acceptance.md)。
