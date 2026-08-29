# MCAP FINALIZING 压缩验收

- 日期：2026-08-29
- 工作站：W3
- Revision：`26acea7`
- 模式：临时 RGB-only、默认 Zstd 压缩
- Artifact：`/home/lenovo/swy/reports/2026-08-29/mcap-compression-validation`

## 结果

连续三条 success Episode 均完成同步 `FINALIZING`、`mcap doctor`、项目 Stream 审计、原子替换和 metadata 提交：

| 时长 | 原始大小 | 最终大小 | 空间缩减 | 压缩 + doctor |
| ---: | ---: | ---: | ---: | ---: |
| 26.71 s | 2.934 GB | 1.098 GB | 62.6% | 20.2 s |
| 45.34 s | 4.958 GB | 1.872 GB | 62.2% | 33.6 s |
| 44.93 s | 4.913 GB | 1.785 GB | 63.7% | 32.6 s |

合计从 12.81 GB 降至 4.75 GB，节省 62.9%。双臂约 1000 Hz，三路 RGB 约 29.99 Hz；所有流的 Header 时间戳单调，消息数、Topic、类型和频率审计通过。

FINALIZING 期间反复输入两个踏板没有启动下一条 Episode；重新出现 READY 后的新输入才生效，Trigger 隔离通过。

## Doctor warning 处置

MCAP 原始写入由多 Topic 并发回调完成，文件物理顺序允许出现亚毫秒级跨 Topic `log_time` 回退。三条最大回退均小于 1 ms；它不代表单 Topic Header 倒退、重复、遗漏或压缩损坏，`--order preserve` 只保留原始顺序。

运行期仅过滤这一种已识别的 `Message.log_time ... less than the latest log time` 噪声。Doctor 非零退出、其他 stderr、压缩后 Stream metrics 不一致仍保持 fail-closed，不提交 Episode，也不返回 READY。

## 结论

W3 RGB-only 连续采集、同步压缩、结构审计、metadata 和踏板隔离验收通过。四台统一部署后仍需各做一条短 Episode，确认镜像与站点环境一致。
