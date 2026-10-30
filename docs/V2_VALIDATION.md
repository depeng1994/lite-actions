# Lite Actions 2.0 开发与验证记录

## 当前结论

公共 Router / Scheduler / Executor、schema 3、Workflow 默认值与 Python 覆盖、已有容器绑定、结果预算及隔离迁移已实现。候选版本完成自动测试与两区真实 GitHub OS 闭环。**NPU 训练尚未验收，生产尚未切换，PR 保持 draft。**

代码：

- [调度仓 PR #10](https://github.com/depeng1994/lite-actions/pull/10)
- [模型仓 PR #11](https://github.com/torchtitan-npu/torchtitan-npu/pull/11)

## 已执行验证

| 验证 | 实际结果 |
| --- | --- |
| 模型仓相关 CPU/tooling 测试 | 54 passed，24 subtests；不代表 NPU 训练或数值精度验证 |
| 调度仓完整回归 | 已执行本地与两台 Linux worker 回归，最终命令与记录见下方 |
| blue Linux 完整 PID 证明 | 活跃的带标记 CPU 初始化进程即使尚未占用 NPU，也会阻止释放 |
| yellow PID 可见性边界 | NPU 清理要求完整 PID 可见性；yellow 首版只运行 OS 探测 |
| 隔离迁移 dry-run | 保留 1 条隔离记录，映射到 `113.46.17.205:153` 的设备 `0–7`；未清除旧隔离记录 |
| 独立只读复核 | 已修正未绑定节点选择、未分配节点影响日志、旧 attempt 继续运行、接单崩溃丢队列，以及仅凭 NPU 空闲释放资源的问题 |

测试命令：

```bash
# 模型仓，Python 3.12
TORCH_DEVICE_BACKEND_AUTOLOAD=0 python -m pytest \
  tests/unit_tests/tooling/lite_actions \
  tests/unit_tests/test_lite_actions_dispatch_smoke.py \
  tests/unit_tests/models/deepseek_v4/test_nightly_flash_recipes.py -q --tb=short

# 调度仓，本地
python -m pytest tests -q --tb=short

# 调度仓，ycy / hwy 的独立验证目录
python3 -m unittest discover -s tests
```

Linux 的环境跳过包括：相邻模型 checkout 的可选归档测试；yellow 额外跳过要求 root 完整 PID 可见性的清理证明。该清理证明在 blue/root 实际执行，不能将 yellow 的跳过写为通过。

## GitHub 端到端证据

[手动 Smoke 运行 41040977540](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41040977540) 使用模型提交 `5e84f6610064a7092cc596a9f4c3bb64e521ed8c` 和调度仓候选构建 `37f0227`：Prepare、schema 3 Artifact、独立 worker Pull、逐 case Commit Comment、Waiter 及汇总全部通过。

| Worker | 结果 | 摘录行数 | Comment 字节数 | 序列化请求字节数 |
| --- | --- | ---: | ---: | ---: |
| blue / ycy | PASS | 13 | 872 | 988 |
| yellow / hwy | PASS | 18 | 1172 | 1306 |

保持原有每 case 最多 20 行、每行 180 字符、Comment 6100 字节和请求 7000 字节预算。Summary 只展示配置和状态，未追加完整日志或训练产物。

PR #11 也实际自动触发两个新入口：

- [Nightly](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41041716751)
- [Dispatcher Smoke](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41041716633)

OS case 通过。候选配置主动关闭训练资源，因此 NPU case 明确回报 `POOL_DISABLED`，汇总正确失败。**PR 自动触发已确认，完整 NPU CI 通过尚未确认。**

## Lint

已在 hwy 的匹配环境重跑完整 pre-commit：使用兼容系统 Git 的 pre-commit 4.0.1、仓库固定的 Pyrefly 0.45.1，以及 TorchTitan v0.3.0（提交 `086bf6c166ec85c1298eb5596fa9bf95f6a2d840`）。GitHub 与仓库约定的 GitCode mirror 标签 SHA 相同。

YAML、格式、拼写、架构边界、OAT 等检查通过。**完整 Lint 未通过**：Pyrefly 报 192 个错误。对未修改上游基线 `8a71d8940e877cc9f2e5f4b977b4f18d48539592` 使用同一环境与配置复查，同样报 192 个错误，报错集合一致，没有新增错误。未将基线失败改写为绿色结果。

## 部署与剩余条件

- 当前 A3 唯一归属 blue；节点标识统一为 `113.46.17.205:153`、`113.46.17.205:154`。
- ycy 已登记新 SSH 别名，复用既有已验证主机密钥。
- 候选部署位于 ycy `/root/work/lite-actions-v2-candidate`、hwy `/home/developer/lite-actions-v2-candidate`；候选运行配置关闭全部训练池，未替换原生产部署。
- 用户明确选择两节点已有容器 `cann_master_torchtitan_master_xyj`。Workflow 默认 `docker` 使用这个统一环境名，部署配置逐节点绑定容器 ID、镜像 ID 和实际 `image_ref`，不创建或重命名容器。
- 两节点 TorchTitan 均为 0.3.0；`:153` 的 torch_npu 为 2.14.0.dev20261019，NumPy 为 1.26.4，`:154` 分别为 2.14.0.dev20260920、2.5.3。版本差异需要真实 16P 验证，不将同名环境视为配套相同。
- yellow 的两个训练池保持禁用；A5 不声明已验收。

下一阶段保留/核对旧隔离记录、排空旧调度、切换新锁身份，依次执行 8P smoke、两条 DSV4 五步训练、16P smoke、并发和取消验收。通过后才能进行生产入口切换及 required checks 更新。

## 最终回归与候选运行状态

- 本地调度仓：109 passed、6 skipped、61 subtests。
- blue/ycy：完整运行 115 项测试，OK，跳过 1 项。
- yellow/hwy：完整运行 115 项测试，OK，跳过 2 项。
- [Nightly 手动入口的 OS 选择验收](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41044618633)：Prepare、blue/yellow case、Lite-Actions-Nightly 汇总全部成功。此运行选择 worker_probe_tests，不代表默认 DSV4 用例已执行。
- 已禁用 4 个源文件不存在的历史 workflow 注册；有效生产入口随最终硬件验收后切换。
- 候选 OS 验证结束后停止候选 worker，保存候选源码、配置、日志和凭据文件的受限权限；原生产部署继续保持原状态。继续验收时先更新候选代码，再按本文硬件门槛恢复，不直接解除旧隔离记录。

完整 Linux pre-commit 的日志位于 hwy `/home/developer/lite-actions-v2-candidate/linux-precommit-pinned.log`；基线对照日志为同目录 `baseline-pyrefly.log`。完整 Linux 调度测试日志为两台 worker 各自候选根目录下的 `final-validation.log`。
