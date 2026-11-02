# GitCode Forward 实施检查点：尚未完成端到端交付

> **2026-11-02 Blue 生产验收更新**：vLLM GitHub 原生 Blue 默认 OS／8P／16P 已部署并完成手动、个人 PR、真失败恢复及 TorchTitan 兼容回归。详见 [当前 Blue 生产验收](vllm-blue-os-8p-16p-production.md) 与 [逐项机器记录](evidence/vllm-blue-os-8p-16p-production.json)。本文件下方为较早检查点；其 Blue 尚未发布的陈述不再代表当前状态。TorchTitan GitCode 的全量跨平台 DoD 以及已记录的 Base-only 竞争恢复缺口未因此关闭。

> **2026-11-02 范围修订（优先适用）**：vLLM Ascend 只验收 GitHub 原生链路，不再要求 GitCode 个人仓、Bridge、状态回写或 GitCode 控制源同步。TorchTitan GitCode 链路不变。详见 [范围修订与已有 PR 实证](gitcode-forward-scope-20261102.md)；其中同时区分已通过的 Yellow A5 8P 与待验收的 Blue A3。下文相冲突的双 GitCode 仓要求以该修订为准。

本记录区分已提交实现、精确提交回归、只读环境预检与尚未完成的跨平台验收。它不是设计文档 DoD 的通过声明。

## 1. 分支与前置

- 实现分支：`depeng1994/lite-actions:gitcode-forward`。
- 本次精确验证的代码提交：`748239eb43e5ff7fdc85e644773b96996ce90f6a`。
- 对应 Git tree：`24851ffe64d07cef6177e59eb369b790b4545e22`。
- TorchTitan NPU PR #11 已合入，merge commit：`f20d073aec6bb7543b2e7db706d1e01b5298c8ca`。
- Lite Actions Worker PR #14 已合入，merge commit：`0585f4c8fe5658c95c913d0dca48b7ad28f33d4d`；本分支已通过 `c0b69725e426ab690e263c1d2005d235022e2cbb` 纳入。
- 两个前置 PR 已不再阻塞开发；其历史 Smoke/Nightly 结果不计为本次 GitCode 合并源码链路验收。

## 2. 已提交实现与本次收口

分支已有 GitCode 权威 PR/当前 Base 读取、H/B 合并源码构造、安全归档、schema 4/Manifest 2 校验、SQLite 代际与执行认领、nonce 绑定派发、按 ID 定位 Check Run、固定 vLLM 容器身份等独立模块及测试。

本次收口 13 个文件：

- 已撤销的执行不能重新签发 admission receipt。
- 延迟的 admission 重试不能把 aggregate Check 的阶段回退。
- 解压前限制 PAX/GNU 元数据大小，并拒绝 CRLF 格式的未物化 LFS 指针。
- `execution_node` 对 `vllm_hccl_smoke` 核对真实 Name/ID/Running/镜像，捕获绑定；配置记录两节点不同的不可变容器 ID。
- 补充上述负向回归，整理 Bridge/Prepare 格式。

**尚未实现完整接线：** 生产 Router/Executor/SSH/reconcile/结果回报主链尚未消费这些 schema 4 模块，不能据此称 Blue 已支持 GitCode 执行。模型仓控制 Workflow/Prepare/Waiter 和 GitCode 个人仓受信 Bridge/事件补偿也尚未形成发布闭环。

## 3. 本次验证证据

在独立 worktree 检出上述精确代码提交后执行，不以脏工作区结果代替提交验证：

| 检查 | 实际结果 | 验证范围 |
|---|---|---|
| `python3 -m unittest discover -s tests -v` | 145 tests，OK | CPU、Mock 与本地 Git 行为；不是平台 E2E |
| `python3 scripts/lint.py` | All checks passed；47 files already formatted | 全部 47 个已跟踪 Python 文件 |
| :153 容器身份只读预检 | PASS | `vllm_hccl_smoke` Name、ID、Running、镜像及容器内 UID 可见性 |
| :154 容器身份只读预检 | PASS | 同上；独立容器 ID |
| GitCode → GitHub → Blue → GitCode | NOT RUN | 未派发本期真实合并源码任务 |

测试机器报告的验证时间：`2026-11-01T22:19:52.498588+00:00`；容器预检时间：`2026-11-01T22:21:23.070267+00:00`。北京时间均为 2026-11-02。

原始日志保留在授权机器 `/root/gitcode-forward-work/`：

- `exact-head-748239e-tests.log`：SHA256 `b1b72789cec012e41cb384742a8cbeb43bc9854f6a193dee8c27a3d8b9f0f200`。
- `exact-head-748239e-lint.log`：SHA256 `fb368efca97e2f42cff475472ff6e34a19acf8edcab0d37d6f56b3511b57e5bf`。
- `vllm-container-preflight-20261102.json`：记录两台机器的真实容器绑定。本轮没有运行 NPU 用例，不能把这项预检写成 8P/16P HCCL PASS。

## 4. 当前缺口与发布 Gate

| 项目 | 现场事实 / 未完成工作 |
|---|---|
| Blue 集成 | 独立协议、状态和结果模块仍需接入实际发现、Prepare-success 准入、租约、staging、launch、取消、清理、outbox 与恢复链路；还须验证 schema 3 兼容和独占升级/回滚。 |
| 双模型控制面 | 尚无本期完整可信 GitCode 控制发布与两个 GitHub 默认分支 C=U 的核验记录。不能用 GitHub 独有补丁替代，也没有改写镜像策略。 |
| GitCode TorchTitan 个人仓 | 已读到 `depeng1994/torchtitan-npu`，项目 ID `9075802`，默认 `master`；当次查询没有开放 PR。其默认 SHA 为 `c6fb6b956249028a59dfb0cc6f55a83ad44fae3a`。 |
| GitCode TorchTitan 控制源 | 已读到 `cann/torchtitan-npu`，项目 ID `9028822`，默认 `master`，当次 SHA `02819e8fd6035f5577fe66a7e3694f64230cbc14`；本轮未写入上游。 |
| GitCode vLLM 映射 | 当前凭据读取 `depeng1994/vllm-ascend` 返回 HTTP 404。这个结果不能区分不存在与无访问权限；未建立经核验的个人仓及 GitCode 控制源映射，不能按同名猜测。 |
| Checks App | Blue 当前服务环境未配置专用 App 身份/installation token；模型仓 repository-secret 名称只读盘点中 TorchTitan 仅有 `DEPLOY_KEY`，vLLM 为空。未证明组织级继承或其他位置绝不存在凭据；必须完成 Prepare 与 Blue 同一专用 App 的创建/更新 Check Run 能力实证。 |
| 事件与状态闭环 | 真实 GitCode 事件/运行身份、账本持久化、Base-only 补偿、状态回写与版本失效时延仍待平台验证。 |
| 实机验收 | OS、8P、16P、Nightly、真失败、更新取消、跨仓互斥及清理/恢复均未通过本期 GitCode 链路验收。 |

这些缺口同时包含尚未完成的开发工作与外部发布前置；不能把未完成状态仅归因于等待两个前置 PR 或平台权限。

## 5. 按设计的验收状态

每项单独保留未验收状态；独立单测不自动等于 INT 集成验收。

| ID | 状态 |
|---|---|
| E2E-01 | NOT RUN |
| E2E-02 | NOT RUN |
| E2E-03 | NOT RUN |
| E2E-04 | NOT RUN |
| E2E-05 | NOT RUN |
| E2E-06 | NOT RUN |
| E2E-07 | NOT RUN |
| E2E-08 | NOT RUN |
| E2E-09 | NOT RUN |
| E2E-10 | NOT RUN |
| E2E-11 | NOT RUN |
| E2E-12 | NOT RUN |
| E2E-13 | NOT RUN |
| E2E-14 | NOT RUN |
| E2E-15 | NOT RUN |
| E2E-16 | NOT RUN |
| E2E-17 | NOT RUN |
| E2E-18 | NOT RUN |
| INT-01 | NOT ACCEPTED — 独立模块有相关单测，跨阶段验收未完成 |
| INT-02 | NOT ACCEPTED — 独立模块有相关单测，跨阶段验收未完成 |
| INT-03 | NOT ACCEPTED — 独立模块有相关单测，跨阶段验收未完成 |
| INT-04 | NOT ACCEPTED — 生产状态迁移/回滚未验收 |
| INT-05 | NOT ACCEPTED — 真实 reconcile 接线未验收 |
| INT-06 | NOT ACCEPTED — schema 4 跨仓故障隔离未验收 |
| INT-07 | NOT ACCEPTED — Mock 检查不代替同一专用 App 的平台实证 |
| INT-08 | NOT ACCEPTED — 独立模块有相关单测，跨阶段验收未完成 |
| INT-09 | NOT ACCEPTED — 负向单测及真实只读身份预检通过，但全生命周期恢复与实机锁验证未完成 |

## 6. 生产边界

本次观察到 Blue 服务仍 active，发布目录为 `/opt/lite-actions/releases/0ff72385648f0f394dc24c68110e335dcf9d6522`，不是本分支代码。本轮未切换/重启生产 Worker，未调度新 NPU 任务，未修改生产隔离记录、Required Checks、分支保护或 Yellow。未自动合并业务 PR，未修改 GitCode 官方上游。

**结论：基础模块与回归已有可核验提交；跨仓接入、受控发布及全部 E2E/INT 验收尚未完成，不能宣称交付成功。**
