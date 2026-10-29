# Lite Actions 架构重构与开发台账 (DEVELOPMENT LEDGER)

> **文档定位**：本文档为 Lite Actions 架构演进、并发调度设计契约及阶段验收成果的唯一跟踪台账。
> **日常使用与系统架构**：请参阅 [README.md](README.md)；模型测试用例编写请参阅模型仓 [tests/integration_tests/README.md](https://github.com/depeng1994/torchtitan-npu/blob/master/tests/integration_tests/README.md)。
> **最新状态（2026-10-29 刷新）**：
> - **Phase 1（解耦、Matrix、动态选卡、全局串行）**：**已全部完工并通过实机验收**。
> - **Phase 2 / P2.5（资源池并发、全端口隔离、同 Node 多任务、双 16P、4×8P 满载）**：**核心并发功能已全部实机验收并正式上线**。
> - **待完成事项**：N10~N13 破坏性故障注入与高压饥饿压测；GitCode 主源 [MR #985](https://gitcode.com/cann/torchtitan-npu/merge_requests/985) 合并归一；A5 64P 专项接入（继续保持 disabled）。
> **生产代码基线**：调度仓 `lite-actions:main` 当前 HEAD `9069e19`（生产运行基线 `c54c0de` / `9069e19`）；模型仓 `torchtitan-npu:master` 验证基线 `f88d50b`。

---

## 1. 真实代码与系统现状（到底现状是什么样？）

经对 `lite-actions` 当前代码库全面核验，**系统已彻底脱离早期 V1 静态架构，完整实现了 V2/P2.5 动态资源池与多任务并发调度架构**。

### 1.1 核心代码模块现状审查

| 模块 / 路径 | 当前代码实现与架构能力 | 运行状态 |
| :--- | :--- | :---: |
| **[`config/pipelines.json`](file:///Users/depeng/work/work/lite-actions/config/pipelines.json)** | 彻底移除旧版写死设备的 `allocation`，完全基于资源规格声明（`nodes/devices_per_node`），解耦通道与物理卡。 | 生产生效 |
| **[`config/pools.json`](file:///Users/depeng/work/work/lite-actions/config/pools.json)** | 登记 `a3-shared`（`a3-3` 与 `a3-4`），各 16 个逻辑芯片（`card-die` 映射）。登记经实机验证的 4 组 8P Group（`0-7` 与 `8-15`）及 2 组互补双机 16P Bundle（对角配对）。配置 `max_jobs_per_host: 2` 与 4 组 PortProfile。 | 生产生效 |
| **[`ci_core/scheduler.py`](file:///Users/depeng/work/work/lite-actions/ci_core/scheduler.py)** | 1. **候选组动态匹配**：单机 8P 动态选空闲 Host/Group，双机 16P 动态选互补 Bundle；<br>2. **复合原子租约**：同时锁定芯片文件锁、Host 预算（最多 2 任务/机）与通信端口 Slot；<br>3. **大任务优先**：实现 `64P > 16P > 8P` 的调度优先级准入与排队意图预留，防止小任务饥饿大任务。 | 生产生效 |
| **[`ci_core/occupancy.py`](file:///Users/depeng/work/work/lite-actions/ci_core/occupancy.py)** | **宿主机命名空间级外部占用审计**：利用 `npu-smi` 关联 chip 与 PID，核对 `/proc/<pid>/cmdline`、UID 与启动时间；仅定点清理已授权的 `auto-occupy` 与 `vllm` 进程；普通用户进程等待/换卡，严禁误杀。清理后必须重检 NPU 空闲。 | 生产生效 |
| **[`ci_core/dispatcher.py`](file:///Users/depeng/work/work/lite-actions/ci_core/dispatcher.py) & [`protocol.py`](file:///Users/depeng/work/work/lite-actions/ci_core/protocol.py)** | 1. 单个 GitHub Run 拆解为多个 Matrix Case Task，由线程池独立派发；<br>2. **Case 级独立上报**：按 Test ID 单独生成 Commit Comment，Matrix Waiter 独立结束；<br>3. 局部重跑防御，避免跨 Attempt 假 PASS。 | 生产生效 |
| **[`ci_core/ssh_runner.py`](file:///Users/depeng/work/work/lite-actions/ci_core/ssh_runner.py)** | 远端训练进程组管控：传递完整的通信端口租约（`MASTER_PORT`、`HCCL_IF_BASE_PORT`、Host/NPU Socket Range）；记录独立 PGID/PID；超时与 Cancel 定点清理本 Case 进程组。 | 生产生效 |
| **[`ci_core/job_state.py`](file:///Users/depeng/work/work/lite-actions/ci_core/job_state.py)** | 任务状态原子持久化账本（`QUEUED` $\rightarrow$ `RUNNING` $\rightarrow$ `PENDING_REPORT` $\rightarrow$ `REPORTED`）；记录租约所有者与端口 Slot；释放不确定时标记 `QUARANTINED` 隔离。 | 生产生效 |
| **[`unified_agent.py`](file:///Users/depeng/work/work/lite-actions/unified_agent.py)** | 轮询 Agent 支持多 Worker 并发派发（4 个训练 Worker + 1 个 Smoke Worker），通过 Scheduler 仲裁资源。 | 生产生效 |
| **双仓解耦状态** | `lite-actions/github/` 镜像目录及 `scripts/sync_ci_mirror.py` 已**彻底删除**；模型仓作为 Workflow/Waiter 唯一事实来源。 | 已解耦 |

---

## 2. 已完成内容与验收成果（完成了哪些？）

系统历经 Phase 1（串行闭环）与 Phase 2/P2.5（资源池并发），已完成全部既定核心功能的开发与实机验证：

### 2.1 Phase 1 交付成果（基础架构与动态调度闭环）
1. **双仓去重与协议标准化**：彻底清理调度仓的 Workflow/Waiter 副本；通过 Artifact 传递版本化 Schema，单测改用轻量 Mock Fixture。
2. **GitHub Matrix 独立用例展示**：单个 Run 展开为多个独立 GitHub Matrix Waiter Job，每个 Case 独立上报 PASS/FAIL，互不污染。
3. **8P/16P 动态选卡初版打通**：Pipeline 改为资源规格声明；Scheduler 动态选择可用卡组；验证了同一用例在不同 Host 卡组间的动态调度。
4. **外部进程可信定点清理**：宿主机 SSH 定点清理 `auto-occupy` Worker PID（[Run 38060703524](https://github.com/depeng1994/torchtitan-npu/actions/runs/38060703524)），重检 NPU 释放；普通用户进程零误杀。
5. **串行故障与取消安全**：Case 独立进程组与超时清理，GitHub Job Cancel 时安全回收卡资源；异常设备进入 `QUARANTINED` 隔离。
6. **实机回归基线**：新动态选卡下，8P Muon/AdamW（5 steps + TB，[Run 38084585959](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084585959)）与双机 16P（[Run 38084588613](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084588613)）全绿 PASS。

### 2.2 Phase 2 / P2.5 交付成果（真正资源池并发调度）
1. **全端口租约物理隔离（PortProfile）**：
   - 为避免 Host 端口及 NPU 根节点默认 `16666` 冲突，分配独立的通信端口 Slot（Slot 0 / Slot 1）。
   - 完整绑定并向 Rank 透传：`MASTER_PORT`（16000/16001）、`HCCL_IF_BASE_PORT`（20000/20064）、`HCCL_HOST_SOCKET_PORT_RANGE`（24000-24063 / 24128-24191）、`HCCL_NPU_SOCKET_PORT_RANGE`（28000-28063 / 28128-28191）。
2. **跨 Host 2×8P 真实并发**：
   - A3-3 与 A3-4 各承载 1 个 8P，时间真实重叠运行，各自 5 steps/TB 校验全绿（[Run 38147139071 Attempt 3](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139071)）。
3. **同 Node 双 8P 并发与 A3-3 第二卡组解锁**：
   - 实机验证了 A3-3 `8-15` 的独立健康度与可用性；A3-3、A3-4 均开放双 8P 组，Host 容量放量至 `max_jobs_per_host: 2`。
4. **四路 8P 满载并发（N14 PASS，两机 32 卡满载）**：
   - 证据：[PR #6 Run 38176153053 Attempt 2](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153053) + [PR #8 Run 38176477643 Attempt 1](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176477643)。
   - 成果：两台机器同时运行 4 组 8P 任务（a3-3 `0-7`/`8-15`，a3-4 `0-7`/`8-15`），4 个 Case 均 PASS，端口与资源完全隔离，任务完成后资源全部安全释放。
5. **8P + 16P 同节点混部（N04 PASS）**：
   - 证据：[8P Run 38176153053 Attempt 1](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153053) + [16P Run 38176153109 Attempt 1](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153109)。
   - 成果：A3-3 上 8P 与跨 A3-3/A3-4 的 16P 任务同时并发运行，3 个 Case 均 PASS，无重叠 DeviceSet，无端口冲突。
6. **双 16P 跨机并发（N05 PASS）**：
   - 证据：[PR #6 Run 38176153109 Attempt 2](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153109) + [PR #9 Run 38177027448 Attempt 1](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38177027448)。
   - 成果：两套 16P 任务使用互补双机组合 `(a3-3: 0-7 + a3-4: 8-15)` 与 `(a3-3: 8-15 + a3-4: 0-7)`，分别占用 Port Slot 0 与 Slot 1，重叠执行区间内均 PASS。
7. **64P > 16P > 8P 统一优先级调度**：
   - 调度器准入按节点规模优先级排序与意图预留，单测与调度逻辑已闭环。

---

## 3. 待完成内容与演进规划（还有什么待完成？）

当前核心并发链路已全部调通并上线，剩余待办工作聚焦于**极高压力与破坏性容错、主源分支归一、以及超大集群专项**：

### 3.1 破坏性故障注入与可靠性压测（N10 ~ N13）
- [ ] **N10 并发取消与孤儿 Rank 清理**：在高并发运行状态下，主动取消其中一个 8P 或 16P 任务，验证远端进程定点终止且绝对不影响同节点其他并发 Case。
- [ ] **N11 Dispatcher 崩溃重启恢复**：在并发任务执行过程中强杀调度 Agent，重启后验证基于远端 `/proc` 与 marker 的对账恢复逻辑，不产生脏卡复用。
- [ ] **N12 外部进程重生与极端竞争**：针对外部看门狗进程快速重生的极端情况进行有限重试与隔离验证。
- [ ] **N13 50~100 模拟任务高压饥饿与公平性测试**：模拟大批量 8P 小任务持续提交时，16P 大任务是否能通过意图预留机制按优先级正常准入，不被持续饥饿。
- [ ] **灾难恢复与回滚演练**：演练在多并发活跃状态下一键切回串行或上一稳定版本的平滑回滚流程。

### 3.2 GitCode 主源归一与自动化同步
- [ ] **GitCode MR #985 合并**：模型仓 V2 Workflow 与 Waiter 变更已提交至 [GitCode MR #985](https://gitcode.com/cann/torchtitan-npu/merge_requests/985)，需由具备权限的维护者合入主源。
- [ ] **双主源对账与恢复 Sync Upstream**：核实 GitCode `master` 与 GitHub `master` 提交一致性，重新启用每日定时镜像同步，解除人工禁用状态。

### 3.3 A5 64P 超大集群专项接入（独立项目）
- [ ] 核实 A5 8 台机器真实的内部 SSH 指纹、网络拓扑与 HCCL 小网 IP。
- [ ] 模型仓配置真实 A5 模型资产路径与 CANN 环境。
- [ ] 按照“单机探测 $\rightarrow$ 多机 1 step $\rightarrow$ 8 机 5 steps + TB”的流程独立开展硬件专项验收；验收前保持 `enabled: false`。

---

## 4. 全量验收测试矩阵状态追踪

| 编号 | 类别 | 测试场景 | 通过条件 | 当前状态 | 关键证据 |
| :---: | :---: | :--- | :--- | :---: | :--- |
| **F01** | 功能 | CPU-only Suite 展开 | 纯净环境依赖 SHA 展开 Matrix，无 NPU 依赖 | **PASS** | `test_matrix_dispatch.py` |
| **F02** | 安全 | 非法 Actor/分支准入防御 | 非白名单提交 fail-closed，绝不下发 SSH | **PASS** | 严格 master/PR 绑定校验 |
| **F03** | 功能 | Matrix 独立 Job 派发 | 拆解为唯一 Task，每 Case 独立 Commit Comment | **PASS** | [Run 38084585959](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084585959) |
| **F04** | 调度 | 串行/并发槽位管控 | 严格遵守并发度与 Host 容量限制 | **PASS** | 调度器 Worker 线程池 + Host 锁 |
| **F05** | 协议 | 错配/延迟 Comment 过滤 | 精确匹配 Test ID、Run ID、Attempt，防读历史假结果 | **PASS** | `test_log_reporting.py` |
| **F06** | 资源 | 动态 8P/16P 原子选卡 | 选用已验证拓扑，多机原子锁定，拿锁失败整体回滚 | **PASS** | `test_phase1_v2.py` |
| **F07** | 进程 | 外部进程定点清理 | 仅清理可信 `auto-occupy`/`vllm`，普通用户不杀 | **PASS** | [Run 38060703524](https://github.com/depeng1994/torchtitan-npu/actions/runs/38060703524) |
| **F08** | 进程 | 误杀与重生防御 | PID 命名空间混淆时 fail-closed，重生有限重试隔离 | **PASS** | `test_phase1_recovery.py` |
| **F09** | 容错 | Job Cancel 进程清理 | 取消仅清理本 Case 进程组，释放未确认标记隔离 | **PASS** | [Run 38054570924](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054570924) |
| **F10** | 实机 | A3 动态 8P 双用例 | Muon + AdamW 5 steps + TB，两个独立 Matrix Job PASS | **PASS** | [Run 38084585959](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084585959) |
| **F11** | 实机 | A3 动态 16P 双机 | 动态选卡下两机 EP16 5 steps + TB + 释放确认 | **PASS** | [Run 38084588613](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084588613) |
| **F12** | 安全 | A5 禁用安全门禁 | `enabled: false`，缺少配置直接拒绝，绝不上机 | **PASS** | `test_a5_onboarding.py` |
| **F13** | 架构 | 双仓解耦与平滑回退 | 调度仓无冗余镜像，具备向后兼容与一键切回能力 | **PASS** | [Run 38060151936](https://github.com/depeng1994/torchtitan-npu/actions/runs/38060151936) |
| **C01** | 并发 | 跨 Host 2×8P 并发 | 两机各承载 1 个 8P，时间真实重叠，双 PASS | **PASS** | [Run 38147139071](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139071) |
| **C02** | 并发 | 全端口租约隔离 | Slot 0/1 绑定 Master/HCCL Base/Host/NPU Socket Range | **PASS** | 实机抓包与 Rank 环境变量验证 |
| **C03** | 并发 | 同 Node 双 8P 真实并发 | A3-3 / A3-4 单机跑两个 8P，内存与端口互不冲突 | **PASS** | 独立探针与 N14 验证 |
| **C04** | 并发 | 4×8P 两机满载并发 (N14) | 两机 4 组 8P 同时并发运行，32 卡满载全绿 PASS | **PASS** | [Run 38176153053](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153053) + [38176477643](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176477643) |
| **C05** | 并发 | 8P + 16P 同机混部 (N04) | A3-3 上 8P 与跨机 16P 同时并发运行全绿 PASS | **PASS** | [Run 38176153053](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153053) + [38176153109](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153109) |
| **C06** | 并发 | 双 16P 互补跨机并发 (N05) | 两组双机 16P 互补配对，Slot 0/1 重叠区间全绿 PASS | **PASS** | [Run 38176153109](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38176153109) + [38177027448](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38177027448) |
| **C07** | 调度 | 64P > 16P > 8P 优先级 | 大任务意图预留，高优先级准入 | **PASS** | 调度器单测与调度逻辑闭环 |
| **N10** | 容错 | 并发 Cancel 进程隔离 | 并发下取消单一 Case 不误杀同机其他 Case | **待专项压测** | 基础 Cancel 已通，并发压测待补 |
| **N11** | 恢复 | Agent 崩溃重启租约恢复 | 崩溃重启后自动对账远端残留并安全回收 | **待专项压测** | 离线单测通过，实机崩溃注入待补 |
| **N13** | 压力 | 50~100 Case 防饥饿高压 | 大批量小任务下大任务正常调度不饿死 | **待专项压测** | 算法已实现，海量用例压力待补 |

---

## 5. 核心风险控制与运维准则

1. **同 Node 并发不交叠铁律**：
   - 物理芯片集（DeviceSet）绝对不交叠；
   - 端口集合（PortSet）绝对互斥（由调度机分配独立的 PortProfile，禁止各任务使用默认固定端口）；
   - Host 最大并发任务数强制受 `max_jobs_per_host: 2` 约束，严禁超配。
2. **外部进程定点清理防御**：
   - 仅限已授权的可信进程名（`auto-occupy`、`vllm`）；
   - 必须在宿主机命名空间下校验目标 PID 的 cmdline、UID、启动时间；
   - 发送 SIGTERM 后限时重检，必要时 SIGKILL，重检确认 `npu-smi` 释放后方可调度；
   - 遇到普通用户进程绝不 Kill，自动换组或排队。
3. **主源同步防覆盖安全**：
   - GitHub `master` 的 `Sync Upstream` 在 GitCode [MR #985](https://gitcode.com/cann/torchtitan-npu/merge_requests/985) 正式合入前**严禁手动开启**，防止 GitHub 端 V2 配置被上游旧代码覆盖。
4. **阻断回滚触发线**：
   - 一旦发生任务间端口冲突（如报 `EI0020` 端口绑定错误）、跨任务误杀、旧 PASS 冒充新结果、或设备残留未清理即复用，立即停止接单，走回滚预案安全降级。
