# w3 π0.5 JAX 推理环境与验收计划

- Status: `files-verified-driver-blocked`
- Host: `w3-arx5`（RTX 5090，Ubuntu 24.04）
- openpi commit: `15a9616a00943ada6c20a0f158e3adb39df2ccac`
- Default policy checkpoint: `9999`（10,000-step 训练的最终 zero-based checkpoint）

## 目标与边界

w3 只部署固定版本 openpi、JAX 运行环境、π0.5-base 资产和本轮 SFT 推理权重，用于离线推理、policy server 和受保护的真机验收。部署不修改采集主线，不读取或改写 MCAP，也不占用现有采集、数据集和 feat 工作树。

独立根目录固定为：

```text
/home/lenovo/swy/pi05-runtime/
  workspace/openpi/                       # 官方代码、固定 .venv
  workspace/ARX5-dual-collection/         # 项目侧适配与验收脚本
  runtime/python/cpython-3.11.15-linux-x86_64-gnu/
  data/pi05/models/                       # pi05_base、tokenizer、fresh stats
  data/pi05/checkpoints/pi05_arx5_joint_sft/
  data/pi05/cache/
  data/pi05/logs/
```

云端 `/workspace` 和 `/mnt/cfs/data/swy/pi05` 只是资产来源；w3 运行时没有 `/mnt` 依赖。

## 已部署组件

1. 云端已验证的 openpi 工作树和完整 `.venv`，保持 JAX 0.5.3 与 NCCL 2.26.5 兼容覆盖。
2. π0.5-base 完整参数、PaliGemma tokenizer 和本数据集 fresh norm stats。
3. `5000`、`9999` 两个 SFT checkpoint 的完整 `params`、`assets` 和 metadata。
4. 项目侧 joint-only ARX5 adapter、环境入口和自动验证脚本。

w3 当前定位为推理设备，因此没有复制两个 checkpoint 各约 30 GiB 的 `train_state`。该目录只包含 optimizer/EMA 等续训状态，不参与 openpi policy 参数加载；如需在 w3 resume 训练，应另行同步，不能把当前部署误认为可续训 checkpoint。

## 主机前置门槛

w3 已识别 PCI `10de:2b85`；[NVIDIA 595.45.04 支持列表](https://download.nvidia.com/XFree86/Linux-x86_64/595.45.04/README/supportedchips.html)将其标识为 RTX 5090。Secure Boot 已关闭，但 NVIDIA 内核驱动尚未安装。由具备 sudo 权限的操作者执行：

```bash
sudo apt-get update
sudo apt-get install -y nvidia-driver-595-open
sudo reboot
```

重启后先确认：

```bash
nvidia-smi
```

驱动未完成前只能验收文件、Python 和 import，不能宣称 JAX GPU 推理就绪。

RTX 5090 属于 Blackwell SM 12.0；[NVIDIA 从 CUDA 12.8 开始提供 Blackwell 工具链支持](https://developer.nvidia.com/blog/cuda-toolkit-12-8-delivers-nvidia-blackwell-support)。当前先原样保留云端已训练验证的 JAX 0.5.3 环境，并以实际矩阵和模型加载为准。若它在 w3 明确报 SM 12.0/CUDA codegen 不兼容，则保留该环境作为可追溯基线，另建 Blackwell 兼容 overlay 验证；禁止直接覆盖并把未经模型回归的升级称为同一环境。

## 环境入口

```bash
source /home/lenovo/swy/pi05-runtime/workspace/ARX5-dual-collection/scripts/w3/pi05_env.sh
python "$ARX5_WORKSPACE/scripts/w3/verify_pi05_runtime.py"
```

驱动安装前可用 `--allow-cpu` 做文件与 import 预检。正式验收必须去掉该参数，输出应包含 `status: ready`、`backend: gpu`、固定 openpi commit、32 维模型 action 和 50-step horizon。

## 验收顺序

1. **资产完整性**：对 openpi、models、两个推理 checkpoint 与云端源执行 checksum dry-run，结果必须为空；确认最终 checkpoint 为 `9999`。
2. **JAX GPU**：`nvidia-smi` 正常，JAX 只需识别 w3 单卡 GPU，并完成矩阵计算；不要求云端八卡 collective。
3. **模型加载**：分别加载 π0.5-base 和 `9999` SFT 参数，禁止联网回退或从用户 home 隐式下载资产。
4. **离线样本**：从已对齐 LeRobot 数据集中读取真实观测，完成 SFT policy 推理；输出必须 finite，形状为 `(50, 14)`，语义为双臂 joint-only，夹爪保持 `0=open, 1=closed`。
5. **服务链路**：启动 openpi policy server，由本机 client 发送同一离线观测，核对直接调用与 RPC 输出契约一致。
6. **真机 shadow**：接入实时相机和关节状态但不下发动作，观察延迟、连续性、关节目标范围和夹爪方向。
7. **受保护真机**：从低速、单步、可急停开始；底层必须实施 joint limit、单步增量/速度限制、超时停机、通信失联停机和人工急停。未通过 shadow 与底层保护测试前，不允许把模型原始 action 直接发给机械臂。

## 完成判定

文件 checksum、JAX GPU、base/SFT 加载、真实样本离线推理和 policy server 五项全部通过，环境才标记为 `inference-ready`。真机验收是独立安全阶段，不是环境部署完成的必要条件。

base 与 SFT 权重加载、真实样本推理合并为一个可重复 smoke：

```bash
source /home/lenovo/swy/pi05-runtime/workspace/ARX5-dual-collection/scripts/w3/pi05_env.sh
python "$ARX5_WORKSPACE/scripts/w3/smoke_pi05_policy.py"
```

## 当前验收结果

- openpi：51,879 个普通文件，源/目标内容树 SHA-256 均为 `e28c04158791885073294331d894a7354a0ea4e22ed41b1eae534c1cd2b19822`；symlink 树也一致。
- Python 3.11 runtime：源/目标内容树 SHA-256 均为 `ee70b7ea6da8b6b2370d1ed26a9da0996042ade50a6d856215ee912765738187`。
- models：26 个文件，源/目标内容树 SHA-256 均为 `226ecbad727a24ebce53e0b22e29c081e0da59f71022a230619c712dcad4283d`。
- 5000/9999 推理 checkpoint：排除 `train_state` 后共 40 个文件，源/目标内容树 SHA-256 均为 `65d729d8fb552f2909ede724720cdbd7299b04af16c615d4553bccbbfd3a73db`。
- w3 占用约为 openpi 8.0 GiB、runtime 100 MiB、models 12 GiB、checkpoints 24 GiB；根盘仍有约 1.1 TiB 可用。
- `.venv` 已从云端绝对路径重定位到隔离根，重复执行重定位脚本通过；固定 commit、JAX 0.5.3、ARX adapter、32 维 action、50-step horizon、5000/9999 资产预检通过。
- 当前 JAX backend 为 CPU，原因是 NVIDIA 驱动未安装；因此 base/SFT GPU 加载、真实样本推理与 policy server 仍待驱动重启后验收。
