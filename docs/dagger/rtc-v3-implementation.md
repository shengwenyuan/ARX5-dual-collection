# π0.5 v3 Training-time RTC 实施计划

## 目标

在既有 Take-over DAgger 上加入连续异步推理。Client 执行当前已验证 action sequence；到达 prefetch 边界后用真实 observation 和已执行 action prefix 提交一个请求，同时继续执行安全尾部。新结果通过 correlation、delay 与 action safety 校验后，原子替换尚未执行的 tail。

## 进程与协议边界

Policy Server 不感知 Client queue、剩余动作、已执行位置或 splice 点。请求最小新增：

- `estimated_delay_steps`
- `action_prefix`：与 observation 对应的 robot-space 14D absolute action prefix

响应保持关联字段、50-step action 与服务端计时。`session_id + episode_id + control_epoch + inference_id + checkpoint_sha256` 是完整关联键，不新增 action sequence wire 字段。

普通推理不增加 MCAP Topic，也不复制模型输入。Session JSONL 记录 submit、estimated/actual delay、RTT、accept/drop、splice 与 underrun。人工介入仍只由 `/dagger/authority` 和 metadata 控制区间表达。

## 单一 typed profile

所有具体数字由一份 TOML profile 加载，禁止在 Scheduler、Client、Server 或 Application 中重复硬编码。profile 内部分为 checkpoint-bound 与 rollout/runtime 两类，但由同一个 `DaggerCollectorSettings` 一次校验。

首版 checkpoint-bound 合约：

- v3 checkpoint tree SHA-256：`c5a2660f139fac5363ec95ec63da3f8c6b372098215ea64612539316fe5a70aa`
- action：robot-space absolute 14D，horizon 50，25 Hz
- gripper：沿用 station profile 的左右归一化契约
- hard-prefix delay：`[0, 10)`；flow steps 10
- camera mapping：overview→`cam_high`，left→`cam_left_wrist`，right→`cam_right_wrist`
- Collector 输入：640x360、RGB、uint8、CHW、INTER_AREA、无 crop/pad
- Server 模型预处理：32D internal action padding，官方 OpenPI transform 与 `resize_with_pad(224, 224)`

首版 rollout：

- `prefetch_after_steps = 10`
- `initial_delay_steps = 3`
- `delay_history_size = 10`
- estimator：rolling maximum
- 最大合法 actual delay 由训练范围派生为 9
- 首轮 action 安全验证窗口由 `prefetch_after_steps + max_delay_steps - 1` 派生为 19，不重复配置

## 调度状态

1. Bootstrap：fresh observation，无 prefix；结果至少验证派生安全窗口后才开放 policy control。
2. Execute：严格以 profile 的 25 Hz 发出已验证动作。
3. Prefetch：发出第 10 步后捕获 fresh observation；以本地 issued count 为 request anchor，提交 estimated delay 和对应 action prefix。
4. Overlap：继续执行旧 sequence 的已验证安全尾部，不等待、重复或插值。
5. Replace：响应到达时计算 `actual_delay = issued_now - issued_at_submit`；仅接受 `[0, 10)`，再从新 chunk 的 actual-delay 位置构建并校验新安全窗口，最后原子替换 tail。
6. Fail closed：queue 在新安全窗口准备好前耗尽、deadline 超限、correlation/epoch/action safety 失败，均关闭 gate 并进入既有故障路径。
7. Take-over：关闭 gate、epoch++、清空 queue 和 in-flight；恢复必须重新 Bootstrap，不复用人工介入前的任何结果。

## 验收

- 单元测试覆盖 bootstrap、单在途、prefetch、0/9/10-step delay 边界、rolling maximum、原子替换、underrun、安全窗口、旧 epoch 和 Take-over 作废。
- 协议测试覆盖 prefix shape/dtype、最小字段与 profile handshake。
- v2 sequential profile 不回归；v3 通过独立 Scheduler 组装，不继续膨胀 CLI 或 v2 Gateway。
- W3 首次模型与动作测试必须由用户启动并在真机旁监督。

## 2026-08-20 静态候选结果

- 新增 checkpoint-bound 与 rollout typed profile。v2/v3 正式 TOML 均显式管理 action、频率、图像、delay、flow、安全和超时数字；Scheduler 只保留派生公式。
- 新增独立 `RtcActionScheduler`：单在途请求、rolling-max delay、19-step 安全窗口、原子 tail replacement、hard-prefix round-trip、epoch 作废、watchdog/underrun fail-closed 与 Session JSONL。
- 最小 wire request 只增加 `estimated_delay_steps` 和 robot-space `action_prefix`；Server profile handshake 会拒绝 checkpoint 合约漂移。
- Server 按真实 v3 训练配置恢复 `use_delta_joint_actions=True`，在输入 transform 内将 robot-space prefix 转入模型空间，输出仍为 Gateway 使用的 robot-space absolute 14D。
- v3 训练 commit 只存在 W3 本地仓库。正式 Policy image 固定官方 OpenPI `15a9616`，再应用与训练 commit `a53d2a3` 对齐的最小 `Pi05ActionPrefixModel`/Gemma 补丁，构建不依赖工作站源码目录。
- 本地全仓 `266 passed, 2 skipped`。W3 Collector image `sha256:8377946ed03ed3f80efc7879ff9ba6b1e1e78d864c211b2ccbd353514077f98c`，容器内 75 项 DAgger 测试通过。
- W3 Policy image `sha256:6ea9f17147fa0d300300806c00fa3c6f799e7060d7f51a925025b44cd00abb43`；v3 model、Server 与 profile 静态 import 通过。
- 全程未加载 checkpoint、未启动 CAN/相机、未采集 Episode、未创建动作 Publisher。下一步由用户启动 Policy Server，先验收 checkpoint load、双路径 warm-up 和 Collector profile handshake，再进入真机 Take-over。

## 2026-08-20 Policy-only 验收

- 用户在 W3 启动独立 Policy 容器；未连接 CAN、相机或动作 Publisher，也未采集 Episode。
- checkpoint tree SHA-256 验证为 `c5a2660f139fac5363ec95ec63da3f8c6b372098215ea64612539316fe5a70aa`；6.2 GiB 参数恢复完成。
- v3 Policy warm-up 完成，WebSocket 在 `0.0.0.0:8000` 就绪；Collector profile handshake 通过。
- JAX 对 ROCm/TPU 的初始化失败只是未使用后端探测，不影响本机 NVIDIA/JAX 路径。
- 首次启动暴露出 Paligemma tokenizer 仍从 GCS 下载。它已改为 Policy image 构建期固化，正式采集启动不再依赖外网。
- 新镜像已在 W3 以 `--network none` 验证 tokenizer 存在且可读；未启动 Policy Server。
- 新增独立 RTC Policy probe：只经 WebSocket 依次执行 bootstrap 和带 3-step prefix 的 RTC 请求，校验关联键、50×14 action 与 hard-prefix 往返误差；它不导入 ROS，也不访问或控制设备。

当前状态：Policy-only checkpoint/profile 验收通过；下一步先做无机械臂、无动作发布的两轮 RTC 协议验证，再进入受监督 Take-over。

## 2026-08-20 RTC wire 验收

- 用户在 W3 以新镜像重新启动 Policy；运行期未再下载 tokenizer。
- checkpoint 自带 norm stats 从 `assets/local/stacking_five_paper_cups_pi05_v2` 正确加载。首次 warm-up 的 cuDNN 算法选择产生 slow-operation 日志，完成后不再影响服务。
- 无设备 Policy probe 经真实 WebSocket 完成 bootstrap 与 3-step hard-prefix RTC 两轮请求：bootstrap `0.092 s`，RTC `0.081 s`。
- hard-prefix robot-space 往返最大误差 `0.00000009`，低于 profile 阈值 `0.00001`；关联键与 50×14 action 校验通过。
- probe 未连接 ROS、CAN、相机或动作 Publisher。Ctrl+C 时 WebSocket Server 已先完成 closing/closed；随后显示的 `KeyboardInterrupt` 来自上游 `serve_forever()`，不代表资源回收失败。

本级结论：checkpoint、checkpoint-bound profile、最小 RTC wire contract、真实 v3 prefix conditioning 和离线启动全部通过。尚待真机验收真实 observation、单在途异步 prefetch、delay 估计、tail 原子替换、underrun fail-closed 与 Take-over epoch 作废。

真机候选必须同时显式指定 `ARX5_DAGGER_POLICY_IMAGE` 与 `ARX5_DAGGER_COLLECTOR_IMAGE`，防止 Compose 复用旧的通用 `:dagger` Collector tag。

## 2026-08-20 W3 supervised Take-over 验收

单个长生命周期 Session `20260820T071729Z` 连续完成两条 success Episode：

- Episode `20260820T071752420531Z-dd9f7fb6`：51.09 s、6.9 GiB、2 次人工介入、9 个 `/dagger/authority` 事件。
- Episode `20260820T071900226500Z-54d092c3`：91.71 s、12.5 GiB、3 次人工介入、13 个 `/dagger/authority` 事件。
- 事件 sequence 在 Session 内从 1 连续到 22；control epoch 从 0 连续到 6。每次均严格为 `TAKEOVER_REQUESTED → HUMAN_ACTIVE → RESUME_REQUESTED → POLICY_ACTIVE`，无 `FAULT_HOLD`；metadata 的 model/human 区间与 MCAP 事件时间一致。
- 7 个 model 区间均独立 bootstrap。RTC 共提交 131 次增量请求、接受 129 次；实际 delay 为 2 steps 128 次、3 steps 1 次，均在训练范围 `[0, 10)` 内。所有 splice 恢复为 19-step 安全窗口，队列最低 6 steps，无 underrun。
- 两个未接受请求分别在 Take-over 和 Episode stop 后立即被 epoch reset 作废；没有旧 epoch response 被接受或继续发出动作。
- 共发出 1588 条控制命令；各 epoch 平均周期 39.94–40.00 ms，最大间隔 49.20 ms，符合 25 Hz 且低于 120 ms watchdog。
- 八路无重复或非单调 Header。双臂约 1000 Hz，三路 RGB-D 约 30 Hz。三相机最近邻配组最大 span 分别为 22.53 ms、27.39 ms，均无超过 40 ms 的组。
- 每条右相机各有 1 个 color-only 边界帧；第二条左右相机出现单次约 66.7 ms 配对间隔。它们不影响本轮 RTC 验收，但应由后处理质量分级保留记录。

核心结论：真实 observation、异步 prefetch、rolling delay、原子 tail replacement、Take-over epoch 作废、控制事件与数据落盘通过。

编排遗留：Collector 正常 `Exited (0)` 后 Policy 容器仍保持 healthy，尚未满足 Session 统一回收；Vendor X5Controller 在 SIGINT 关机尾段记录 `exit code -11`。前者必须在生产入口收口，后者作为 Vendor shutdown 问题跟踪，不否定本轮 Episode 数据。

## 生产入口收口

Policy 与 Collector 保持两个职责独立的容器。Docker Compose 是 host 侧唯一进程编排入口；Collector 仍是采集 Session 主进程，不获取 Docker socket，也不在 `arx5-collect` 内实现容器管理。

环境文件只记录镜像引用、Policy TOML、checkpoint root、Collection TOML 和模式；参考 `config/runner/dagger.env.example`。正常运行必须传本次任务的绝对输出目录，Session 日志自动进入其 `logs/` 子目录：

```bash
ARX5_OUTPUT_ROOT=/home/lenovo/swy/reports/<date>/<task> \
docker compose \
  --env-file /var/lib/arx5-collection/dagger.env \
  -f config/runner/compose.dagger.yaml \
  up --no-build --abort-on-container-exit --exit-code-from collector collector
```

`--abort-on-container-exit` 保证 Collector、Policy 任一退出都会停止整个 Compose 应用组；`--exit-code-from collector` 使生产入口返回采集 Session 的退出码。正常使用不再逐项传镜像、checkpoint、Policy、Task、模式和报告宿主路径。Compose 的默认模式改为 `takeover`；Shadow 仅作为开发诊断显式覆盖。

## 与 `pi05-arx5-inference` 的关系

当前生产入口不复用相邻仓库的 `run_policy_training_rtc_window.sh`，Policy 镜像也不 import 或挂载 `pi05_arx5_inference`。本仓库独立启动 `arx5_collection.collection.dagger.policy_server`，再使用固定 OpenPI 基线、v3 模型扩展和同一 checkpoint 构造 Policy。

相邻仓库提供了早期双窗口运行效果与 training-time RTC 参考；本仓库保留模型语义，但重建了适合采集系统的边界：checkpoint SHA-256、typed profile、最小 `estimated_delay_steps + action_prefix` 请求、完整 correlation key、epoch 作废和 Session 统一编排。后续若抽取共享组件，应只共享稳定的模型 adapter/协议库，不复用工作站 shell 入口或机器人 Runtime。

## W3 后续清理边界

W3 最终只保留一个当前发布入口目录：`/home/lenovo/swy/ARX5-dual-collection-rtc`。工作站身份、Policy 配置和 deployment paths 留在 `/var/lib/arx5-collection`；checkpoint 与 reports 独立于源码目录保存，升级源码不得搬移或删除数据。

本轮不执行目录或镜像清理。正式清理前必须先只读列出候选目录、Git 状态、活跃进程/容器和磁盘占用，再由用户确认精确删除清单。不得触碰并行任务使用的 worktree、`pi05-runtime`、checkpoint、reports 或未合并修改；新入口完成同一镜像与 station config 的一次启动验收后，旧候选目录才可回收。
