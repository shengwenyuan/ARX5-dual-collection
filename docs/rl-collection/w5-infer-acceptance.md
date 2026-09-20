# w5 infer 真机验收入口

2026-09-20：当前选用的标准 RTC slot-4 checkpoint 和统一配置入口见 [统一配置接入](unified-policy-config.md)。下文保留之前 fold-cloth 的手工配置示例，不代表本次选定模型。真机仍由用户启动。

## 已配置的挂载

`arx5 infer` 读取 `/var/lib/arx5-collection/dagger.env`，使用既有 DAgger Compose 加 `compose.infer.yaml`。不需要手工启动第二个模型容器。

| 宿主机路径 | 容器路径 / 用途 |
| --- | --- |
| `/var/lib/arx5-collection/infer-policy.toml` | 两侧只读 `/config/policy.toml`，推理/采集运行配置 |
| `/home/lenovo/swy/pi05-runtime/data/pi05/checkpoints/pi05_arx5_joint_train_rtc` | policy-server 只读 `/checkpoints` |
| `/home/lenovo/swy/ARX5-dual-collection/config/task.rgb-only.json` | 加 `--rgb-only` 时 collector 只读 `/config/task.json` |
| 同目录 `task.eight-stream.json` | 不加 `--rgb-only` 时使用，包含深度流 |
| `/home/lenovo/swy/reports` | collector 中同路径，保持既有采集目录 |

当前 TOML 指向本站已有模型：

```toml
[policy]
checkpoint = "/checkpoints/fold_cloth_arx5_20260826_2_pi05_train_rtc_v1/19999"
checkpoint_sha256 = "659e54c8dbc49f0e32aa2402ce85c7ce40b1fb2fdcafd4e1b53a892546e3d4ea"
repo_id = "local/fold_cloth_arx5_2026-08-26_2"
prompt = "folding the cloth"
```

上面只是字段摘录，不是完整配置。保留磁盘中的完整 TOML。它是本仓库的运行配置，不能直接替换为云训练 TOML。checkpoint 的 step 目录必须同时包含 `params/` 和 `assets/`，包括匹配 repo_id 的 `norm_stats.json`。

## 启动现有模型

从本机进入 w5 后执行：

```bash
ssh -t w5-arx5
cd /home/lenovo/swy/ARX5-dual-collection
export ARX5_DAGGER_POLICY_CONFIG=/var/lib/arx5-collection/infer-policy.toml
export ARX5_CHECKPOINT_ROOT=/home/lenovo/swy/pi05-runtime/data/pi05/checkpoints/pi05_arx5_joint_train_rtc
export ARX5_OUTPUT_ROOT=/home/lenovo/swy/reports/2026-09-20/infer-acceptance
export ARX5_TASK_DESCRIPTION='folding the cloth'
arx5 infer --rgb-only
```

Compose 自动启动模型服务，完成 checkpoint 校验、加载和 warm-up 后启动采集端。无需重新构建或安装依赖。`ARX5_TASK_DESCRIPTION` 写入采集 metadata；实际模型 prompt 由 TOML 的 `policy.prompt` 决定，换任务时需同步修改。

输出根目录必须位于现有 reports 挂载下。若要完整八路传感器采集，将最后一行换为 `arx5 infer`；明确使用本站默认完整任务文件时，先 `unset ARX5_TASK_CONFIG`，避免旧 shell 环境变量覆盖 env 文件。

## 换 checkpoint 或 TOML

1. 复制完整 `infer-policy.toml` 到自己的路径，修改 `policy.checkpoint`、`checkpoint_sha256`、`repo_id`、`prompt`；核对该模型训练时的 RTC horizon、max delay、频率、动作/夹爪及图像契约。不要照搬 w3 的任务模型和频率。
2. `ARX5_CHECKPOINT_ROOT=/宿主机/模型根目录` 会映射为 `/checkpoints`。TOML 里的 checkpoint 必须使用容器路径，例如 `/checkpoints/实验名/27999`；不要填写宿主机路径。
3. `export ARX5_DAGGER_POLICY_CONFIG=/宿主机/完整推理配置.toml`，然后运行同一入口。shell 中的环境变量优先于 `dagger.env`；长期使用可修改该 env 文件的两项路径。

新模型的 SHA 必须按仓库的目录算法计算，不能用单个权重文件的 `sha256sum` 代替。以下仅只读挂载模型，不启动服务或硬件：

```bash
docker run --rm --network none \
  --mount type=bind,src=/宿主机/实验名/27999,dst=/ckpt,readonly \
  --entrypoint /.venv/bin/python arx5-dual-policy:dagger \
  -c 'from arx5_collection.dagger.checkpoint import checkpoint_tree_sha256; print(checkpoint_tree_sha256("/ckpt"))'
```

修改运行 TOML 后可先检查采集端约束：

```bash
docker run --rm --network none \
  --mount "type=bind,src=$ARX5_DAGGER_POLICY_CONFIG,dst=/config/policy.toml,readonly" \
  --entrypoint /ros_entrypoint.sh arx5-dual-collection:infer \
  python3 -c 'from arx5_collection.dagger.config import DaggerCollectorSettings; DaggerCollectorSettings.load("/config/policy.toml"); print("CONFIG OK")'
```

本站新配置的 Snapshot request 为 200 ms，RTC policy wait 为 280 ms、margin 为 50 ms，25 Hz / max delay 10 对应 400 ms 预算。静态校验通过不等于实际推理延迟已通过真机验收。

## 踏板与落盘验收

1. READY 时踩右踏板：GO HOME，然后进入 `INFER RUNNING`。等待模型服务完成 warm-up；HOME 不进入任务动作记录。
2. RUNNING 时右踏板结束为 success，左踏板结束为普通 fail。确认停止动作并完成落盘后，系统停在 READY，不自动 HOME。
3. 释放后重新踩右踏板开始下一局。连续至少三局，覆盖 success、fail 和再启动；每局 `step_index` 从 0 开始。确认 HOME/bootstrap/落盘阶段的输入不会延后触发下一局。
4. 验证长按、释放和再次踩下，不得造成重复开始/结束。现有 HID 没有经验证的 RELEASE 编码，此项必须实测后才能验收通过。
5. READY 时 Ctrl+C 退出；另外单独验证录制中 Ctrl+C 生成 aborted。运行中同批双踏板冲突也是 aborted。

每局保留 `episode.mcap + metadata.json`，默认跳过结束后重写压缩。success 在输出根目录下，普通 fail 在 `infer_fail/`，aborted 在 `abort/`；普通 fail 是有效 RL 数据。检查 `collection_type=infer`、正确 outcome、`extensions.infer.recording_completed=true`，人工 success/fail 的 `termination_reason=human_label`、`errors=[]`。

退出后可打印各局基本结果：

```bash
python3 - <<'PYCODE'
import json, os
from pathlib import Path
for path in sorted(Path(os.environ['ARX5_OUTPUT_ROOT']).rglob('metadata.json')):
    if any(part.endswith('.partial') for part in path.parts):
        continue
    m = json.loads(path.read_text())
    i = m.get('extensions', {}).get('infer', {})
    print(path.parent, m.get('collection_type'), m.get('outcome'),
          i.get('termination_reason'), i.get('command_count'),
          i.get('recording_completed'), m.get('errors'))
PYCODE
```

还需回读 MCAP 的 `/infer/command`：计数与 metadata 一致、步号连续、14D 动作和时钟存在。传感器缺帧、动作对齐与末帧是否适合训练由 exporter 验收；这里只做 collection 真机交接，不将配置检查当作真机完成。
