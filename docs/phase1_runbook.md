# Lite Actions V2 / Phase 1 运维与回滚手册

Phase 1 包含 GitHub Matrix Case 级 Jobs、动态 A3 选卡与**全局至多一个训练 Case**。这不是 Phase 2 并发资源池。Gate 及实机证据以仓库 REFACTOR_PLAN.md 为准。

## 1. 双仓边界与正式准入

**模型仓 depeng1994/torchtitan-npu** 独占 GitHub Workflow、Prepare/Waiter、OverrideDefinitions/build_test_list()、模型参数和 env_vars。**调度仓 depeng1994/lite-actions** 独占 pools/pipelines、设备互斥、全局训练槽、SSH 运行、取消、日志摘取及 Case 结果上报。执行机不存放 GitHub Token，不运行 AI Agent API。

GitHub 正式生产准入为 master + workflow_dispatch + depeng1994。特性分支临时白名单只能用于受控验收，正式发布前必须撤除。GitCode 是模型源码主源，Sync Upstream 每天镜像同名 GitHub 分支；应先将发布提交收录 GitCode master，避免 V2 被镜像覆盖。禁止 force push 丢弃 GitCode 与 GitHub 双方独有历史。

## 2. 部署检查与停收新单

操作前需分别核验：GitHub 没有 in_progress/queued 的相关 Workflow；调度机 state/unified-jobs.json 没有 running/blocked_recovery 或尚未处理的 quarantine；两台 A3 宿主机 npu-smi 与本系统任务 marker；SSH/网络正常；确认一个 Agent 实例及其确切 PID、工作目录、代码 SHA。

**生产唯一 Agent** 仍是运维要求。自 2026-10-11 自审修复版起，V2 使用 OS 用户级、与 worktree 无关的 Agent 单例 flock，并另有同范围的全局训练槽 flock；默认位于当前运行账号的 `~/.cache/lite-actions/`，只有明确运维需要才可通过 `LITE_ACTIONS_AGENT_LOCK` / `LITE_ACTIONS_GLOBAL_SLOT_LOCK` 指向同一绝对路径，禁止不同工作目录配置不同锁路径。旧 V1 不持有这些新锁，因此版本切换仍必须先备份两仓 SHA、Workflow、生产 config/state 与必要日志索引，关闭旧 Agent（仅发送准确 PID 的 SIGTERM），等其退出后才能拉起另一 Agent。运行中的任务不得热迁移、覆盖 state 或删除锁文件。

正式启动仅允许 master，外部自动清理默认关闭：A3_CI_ALLOWED_BRANCHES=master、A3_CI_RECLAIM_EXTERNAL_PROCESSES=0。先运行 python3 -m pytest -q tests 与 python3 unified_agent.py --validate，再启动 ./run-unified-agent.sh 并检查日志与唯一 PID。

## 3. 拓扑、动态选卡和外部占用

已核实单机 8P：A3-3 的 0–7；A3-4 的 0–7 和 8–15。A3-3 的 8–15 尚未验证，不启用。已核实跨机 16P：A3-3 0–7 + A3-4 8–15，两机各 8P。禁止在生产设置 A3_CI_ALLOW_EXPERIMENTAL_TOPOLOGY=1。

Dispatcher 必须先持有全局训练槽，再原子获取整组设备锁，并从对应宿主机只读检查 npu-smi 与进程；跨机不得保留半套锁。候选全部不可用时有界等待或报告容量不足，不得杀普通用户任务。可信 auto-occupy/vllm 只允许对严格核实宿主机 PID、UID、启动时间、类别和租约的进程按 PID 操作；默认不开启自动回收，禁止 pkill -f、killall、全机清理。A5-64P 持续 enabled=false，未经资产路径、网卡与 HCCL 真实验收不得启用。

## 4. 取消、异常恢复与 Quarantine

1. GitHub Cancel 后，Dispatcher 只能终止本次任务。SSH Runner 以本次唯一 LITE_CI_JOB_ID 精确比对 /proc/PID/environ，记录 PID starttime，对 torchrun 和**脱离原 PGID 的所有同标识 rank** 逐 PID 先 TERM、限时后身份未变才 KILL。不能只 kill 启动器进程组，也不能仅凭进程名误杀。
2. 对每台涉及宿主机复查 npu-smi 与本次任务 marker，所有分配的设备都已释放才能释放全局训练槽。如取消中 SSH 失联、释放未确认、PID 不可核实，保留 quarantined=true 与原始日志，严禁错误 PASS 或复用脏设备。
3. GitHub CANCELLED、INFRA_ERROR、RESOURCE_TIMEOUT、FAIL 和 PASS 必须区分。结束广播失败只能记 Warning，不得改写训练已确认的 Exit Code/TensorBoard/设备释放；开始广播仍必须成功。
4. **解除 Quarantine 只能人工审计**：先停收新任务并退出 Agent；核对 Run/Attempt/Test ID、远端 Task 目录、Host PID/启动时间、完整 marker、NPU 占用、相关设备组并记录恢复证据。仅在确认已释放时，把该 Task 的 quarantined 标为 false，同时写 recovery_verified_at 和 recovery_evidence。不能直接清空整个 state 目录。
5. Agent 崩溃后，进程锁释放不代表远端训练停止。running/blocked_recovery 不得被盲重放。GitHub API 故障留下 pending_report 时只重试带原身份的结果上报，不得重跑已完成训练。GitHub 局部 re-run failed jobs 缺少新 Attempt Artifact 时 fail-closed；应完整重触发 Workflow 产生新 Run。

## 5. V1 回滚演练和 V2 恢复

**前置**：所有 Case 完成或按授权清理，NPU 释放可验证；确认没有 GitHub 新任务正在进入。备份当前 Dispatcher SHA、配置/状态、日志、GitHub/GitCode Workflow 提交。

1. 核对并 TERM 当前 Agent 的准确 PID，等待退出，确认只剩零个活跃 Agent。
2. 恢复**备份的 V1 Workflow/白名单**，切回既有 V1 Agent 目录、代码及配置。旧 Run 不热迁移，不擦除 V2 State。
3. 校验 V1 配置，启动单个 V1 Agent；用 lite-actions-dispatcher-smoke.yml 测试云端入口、Agent 领取、Commit Comment/Waiter 终态，记录 Run/SHA。
4. 恢复 V2 也必须先无在途任务、确认全部设备释放，停止 V1 Agent、恢复备份的 V2 模型 Workflow 和 Dispatcher 版本、配置、state，生产白名单仅 master，启动唯一 Agent。
5. 重新执行 V2 Dispatcher Smoke；只有 Run/日志/终态 PASS 才重新接单。任一前置条件失败则停止切换、保存证据并保持隔离。已经终止的训练不可逆，回滚是恢复后续安全服务，绝不是复活原任务。

## 6. 可复现 Gate 证据

- 两仓 CPU 单测、配置校验、双协议兼容/拒绝越权与 Attempt/SHA 错配。
- 两个独立的 8P Muon/AdamW Matrix Jobs，分别 5 steps + TensorBoard + rc=0；动态双机 16P 每 Host 8P、EP16、5 steps + TB、rc=0。
- 跨 Run 同时提交后的**全局串行**时间轴；合法拓扑及设备原子锁；不杀其他用户。
- 中途取消后的远端 marker、完整进程清理和 Host NPU 释放；故障注入安全隔离；V1 回滚 Smoke 和 V2 恢复 Smoke。
- 所有实机 Run URL、模型仓及 Lite SHA、Host 分配、状态和已知风险写回 REFACTOR_PLAN.md；Phase 2 并发不属于 Phase 1 发布范围。
