# 2026-11-02 范围修订：TorchTitan GitCode Bridge + vLLM GitHub 原生 CI

本文件记录用户在 2026-11-02 的明确范围调整，并优先于 `gitcode-forward-design.md` V3.1 及 `gitcode-forward-progress.md` 检查点中与其冲突的 vLLM 要求。不是端到端全部通过的声明。

## 1. 当前有效范围

| 对象 | 有效入口与交付要求 | 不再作为本次前置的事项 |
|---|---|---|
| TorchTitan NPU | 保留 GitCode 个人 PR → GitHub Actions → Blue → GitCode 版本结果的设计；合并源码、可信控制发布、Head/Base 更新失效和取消要求不变 | 无；不因 vLLM 的调整删减 TorchTitan 验收 |
| vLLM Ascend | 只开发、验收 GitHub 原生 PR/手动触发 → GitHub Actions → Lite Actions → GitHub 结果；个人 Fork 为 `depeng1994/vllm-ascend`，控制目标仓为 `cann-framework/vllm-ascend` | GitCode 个人仓、GitCode 上游映射、GitCode Bridge/Action、GitCode 状态回写以及 vLLM 的 GitCode U→GitHub C 同步证明 |
| Lite Actions | 同一 Blue 的共享物理锁、跨仓隔离、取消与清理要求继续保留；schema 3 兼容性不能破坏 | 不得再以 vLLM GitCode 404 阻塞 TorchTitan 或 vLLM GitHub 开发 |

### 对实现和验收的约束

- vLLM 的可信控制代码在 GitHub 开发/审查。只从经验证的可信控制提交执行控制脚本；个人 PR 的业务代码不因此成为可信控制面。
- 保留已有 GitHub schema-3 A5 路径及其历史验证，不要求它先迁移到 GitCode/schema 4。GitCode/schema-4 的专用 Checks App 门槛不能被解释成现有 vLLM schema-3 路径的前置；也不能为绕过 TorchTitan 的 schema-4 门槛而静默回退其结果协议。
- 原设计的 Blue OS/A3 8P/16P、`vllm_hccl_smoke` 固定容器、真实容器 ID、逐节点清理和共享物理设备锁未被本次入口调整取消。旧 Yellow A5 PASS 不替代这些 Blue 验收。
- GitCode 专属的 vLLM 验收项改为 `OUT_OF_SCOPE_BY_USER`，不是 `PASS`。对应的 GitHub 原生验证必须有独立 Run/Attempt/源码版本/Case/清理记录。需要合并结果的场景仍必须证明实际执行的 H/B/M/T，不能把历史 schema-3 Head 测试改称合并结果测试。
- 设计 §6.1、§6.2、VL-01/VL-02/VL-05、E2E-07～10/12/13 中要求 vLLM GitCode 个人仓、Bridge、上游控制源、状态回写和同步的部分由本文件替换；E2E-11 的同一 Blue 双仓互斥仍需验证，不能用 Blue/Yellow 分开运行代替。
- `depeng1994/vllm-ascend` 的 GitCode HTTP 404 仅保留为历史探测事实。用户无需为本次交付创建或修复该 GitCode 仓库。
- 不自动合并业务/验收 PR，不修改 Required Checks、分支保护、生产隔离或 Yellow 服务。本次修订不授权删除旧验证证据。

## 2. 已存在的 vLLM 个人 Fork PR：已核实

### 控制代码 PR #1

- PR：https://github.com/cann-framework/vllm-ascend/pull/1
- 标题：`[CI] Add single Lite Actions A5 8P/32P PR smoke dispatcher`。
- 来源：`depeng1994/vllm-ascend:ci/vllm-a5-smoke`。
- 已合入，merge commit：`e20e2c482606e2ddd6b2fe1bf3a78638f55da8a8`。
- PR 本身加入控制 Workflow 和脚本；真实硬件闭环由后续 PR #2 验证，不能混为同一次验收。

### 真实个人 Fork 验收 PR #2

- PR：https://github.com/cann-framework/vllm-ascend/pull/2
- 标题：`[CI] Verify A5 8P Lite Actions dispatcher from personal Fork`。
- 来源：`depeng1994/vllm-ascend:ci/a5-smoke-pr-validation`。
- 目标：`cann-framework/vllm-ascend:main`；目标仓稳定 ID `1438625957`，个人 Fork ID `1440390070`。
- 当前 PR Head：`bfb61c203ee41f5d7a937bb62f143ff8efbb32ab`，与成功 Run 和结果回报一致。
- PR 仍 open、未合入。它是验收 PR，原描述明确不要合入；open 不等于测试未通过。

## 3. 成功运行与结果证据

| 项目 | 实际核验值 |
|---|---|
| Run | https://github.com/cann-framework/vllm-ascend/actions/runs/41160654371 |
| 成功 Attempt | 2 |
| 事件 | `pull_request_target` |
| Workflow path | `.github/workflows/lite_actions_dispatcher_smoke.yml` |
| Run 状态 | `completed / success` |
| 成功执行时间 | 2026-10-31T03:39:46Z 至 2026-10-31T03:43:17Z；北京时间 11:39:46 至 11:43:17 |
| Prepare | Job `124431562074`，success |
| 8P Waiter | Job `124431598274`，`dispatch_smoke_a5_8p`，success |
| 汇总 | Job `124432273263`，`Lite-Actions-Smoke`，success |
| Worker / Pool | `yellow / a5_shared_yellow` |
| Docker 容器 | `vllm-kimi-wangjinyin`，不是 Blue 的 `vllm_hccl_smoke` |
| 拓扑 | 1 节点 × 8 设备 |
| HCCL 载荷回报 | `DISPATCH_SMOKE_PASS`，`world_size=8`，`all_reduce_sum=28` |
| 最终回报 | `status=PASS`，`exit_code=0`，`cleanup_verified=true` |
| Plan digest | `2adb05faedd7d96e453a918c3a15c22e53f0e4a0936c8016cfeba912addbce9a` |
| 结果评论 | Commit comment `207493712`，同一 Run/Attempt/Head/Case |

结果评论：https://github.com/cann-framework/vllm-ascend/commit/bfb61c203ee41f5d7a937bb62f143ff8efbb32ab#commitcomment-207493712

这是已完成的 GitHub → Yellow A5 8P HCCL 调度、执行、回报与清理基线。该运行没有 32P Job，也没有 Blue OS/A3 8P/16P Job，不能外推为这些场景通过，更不等于完整 vLLM 推理验收。

## 4. 仓库改名与回归边界

历史结果的 `repo` 字段为 `torchtitan-npu/vllm-ascend`，GitHub 当前返回的目标仓为 `cann-framework/vllm-ascend`、Repo ID `1438625957`。保留历史原文，不将它改写成新组织名后伪装为新运行。

本次读取组织仓 `main` 的 `.github/workflows/lite_actions_dispatcher_smoke.yml` 和 `.github/scripts/lite_actions/prepare_request.py`，仍发现旧组织名硬编码。Prepare 的入口会比较 `GITHUB_REPOSITORY` 与旧 `REPO`。因此，历史成功证明 2026-10-31 的运行通过，不证明改名后当前控制代码可直接重新运行成功；发布前须完成 producer/worker/waiter 身份一致性及当前版本回归。

本文件仅记录范围和已核验证据，没有重新调度 A5、修改历史 Run/Comment、合入 PR #2 或切换生产 Worker。GitCode Forward 全项目仍未完成端到端交付。
