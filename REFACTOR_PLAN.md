# Lite Actions V2 重构实施计划

> 状态：**PLAN APPROVED FOR IMPLEMENTATION DESIGN / 尚未开始代码重构**  
> 计划创建：2026-10-10  
> 代码基线（创建计划前）：[lite-actions/main \`2a0427c\`](https://github.com/depeng1994/lite-actions/commit/2a0427ca21fb18e31543480906163492f7a2cfd7)、[torchtitan-npu/master \`b91257e\`](https://github.com/depeng1994/torchtitan-npu/commit/b91257ebb5e9e01f4f76944d523a46c15a740bb0)  
> 本文只定义目标、设计契约、实施步骤与验收门槛；**创建本文不代表代码已经实现或通过验证**。所有实际执行结果必须补录 Run 链接、Commit SHA、测试记录。

## 0. 总目标与边界

### 0.1 目标

将现有「静态 allocation + 以 Workflow Run 为单位顺序执行 + 单个 GitHub Job 汇总」升级为：

1. **资源池与业务请求解耦**：\`config/pools.json\` 管理真实主机、物理 Device ID、SSH、HCCL 网络与经验证的拓扑；\`config/pipelines.json\` 只声明池/可授权的资源规格，不再长期绑定特定主机与 Device ID。
2. **拓扑感知动态调度**：Dispatcher 根据每个可信 Test Case 的 \`ngpu/nnodes\`，结合合法拓扑、实际外部占用与本系统设备租约，原子分配 1～N 个节点的设备；设备不足排队，不抢占他人进程。
3. **Test Case 作为独立调度单元**：相同 GitHub Workflow Run 中的 Muon/AdamW 等 Case 可以并发；不同 Run 同样由一个全局 Dispatcher 仲裁。单机 8P、多机 16P 都必须支持。
4. **GitHub Matrix Jobs**：一个 Case 对应一个原生 GitHub Job 和独立 PASS/FAIL/NOT_RUN 语义；GitHub 不执行实际 NPU 训练，只负责安全请求、等待和 Checks 展示。
5. **代码单一所有权**：TorchTitan-NPU 维护 Workflow、Prepare/Waiter、\`OverrideDefinitions/build_test_list()\` 与用例环境；Lite Actions 维护调度器、资源池、执行器、回传实现。移除 Lite Actions 的 \`github/\` 镜像及同步脚本。
6. **不回归既有能力**：保留已跑通的 A3 8P Muon/AdamW、16P 路线、固定 SHA、TensorBoard 校验、最小权限、调度机统一日志摘取、20 行有界结果回传及物理资源占用防护。

### 0.2 明确不做（V2 范围之外）

- **不**引入 Kubernetes、ARC、GitHub Self-hosted Runner、额外常驻 AI Agent 或模型 API。
- **不**开放不可信 PR/Fork 自动执行；V2 延续已授权用户、已授权仓库及可信 \`master\` SHA 的 \`workflow_dispatch\`。未来 PR 自动检查另立安全评审任务；在 \`master\` 执行的 Matrix Job **不会自动显示在未合并 PR 的 SHA 上**。
- **不**自动 kill 外部用户、\`auto-occupy\` 或其他非本系统拥有的进程；只可安全清理验证为本次任务拥有的进程组。
- **不**开启 A5 64P；保留 \`enabled: false\`，仅允许离线校验配置。不得把示例路径、未验证 HCCL IP 当作实际验收。
- **不**修改模型业务语义、训练 steps、Muon/AdamW 优化器选择、Virtual Optimizer、CANN/HF/Checkpoint 路径归属；这些仍由模型仓测试定义负责。
- **不**把完整训练日志或内部资产搬到 GitHub；输出仍为脱敏且有界的诊断与测试状态。
- **不**在本计划提交时启动任何实机测试、重启调度机或变更生产配置。

### 0.3 术语与指标

- **Physical Device**：\`(host_id, device_id)\` 是全局唯一的物理调度键，需与 \`npu-smi\` 的 \`card-die\` 映射核准。
- **Allocation Group**：同一 Host 上经确认的合法设备组合，不是简单取任意连续 8 个 ID。
- **Lease**：一次 Case 实际持有的设备、端口、Host 预算及进程归属；状态可追踪，释放须有证据。
- **Task ID**：\`repository + workflow_run_id + attempt + pipeline_id + test_id\`；同一 Run 禁止重复 Test ID。
- **理论上限**：A3-3 与 A3-4 各 16 Device，最多 **4 × 单机 8P**；这是待实机证明的上限，**不是当前可保证容量**。当前 \`a3_8p_tests\` 只有两条不同 Case；如需四任务压力测试，使用多个受信任 Run 或新增有业务意义的合法用例，禁止伪造 PASS。
- **稳态验收**：至少证明无设备重叠、独立训练/校验、正确回传、端口隔离、无残留进程；如 Host RAM/IO/HCCL 无法支持同机双 8P，必须回退至每 Host 最大 1 个 8P，并明确报告实际上限为 2，而非伪称 4。

## 1. 已核查现状 / 必须修复的耦合

| 已有实现 | 问题与目标 |
| --- | --- |
| \`config/pools.json\` 已声明 A3-3/A3-4 每机 16 Device，\`process_indexing=card-die\` | 缺少真实可并发运行的拓扑组、Host 内存/端口隔离验收记录 |
| \`config/pipelines.json\` 的 \`allocation\` 绑定固定 Host/Device | 改为只表达池与需求/通道授权；调度结果是动态运行时数据 |
| \`ci_core/resources.py\` 的 \`fcntl.flock\` / \`reserve()\` 已支持按设备键原子非阻塞加锁 | **复用锁原语**，在上层增加拓扑选择、全局队列、资源账本、健康隔离和重试 |
| \`ci_core/dispatch.py\` 的 \`wait_reserve(allocation)\`、\`remote_preflight()\` | 改为根据已决策租约执行；外部占用后释放候选、另选分组；所有失败都 fail-closed |
| \`unified_agent.py\` 的 \`MAX_WORKERS=2\`、Run 级 \`inflight/state\` | 改为 Run 领取与 Case 调度分离；配置有界 Workers，不能仅把常量改成 4 |
| \`ci_core/ssh_runner.py\` 当前逐 Case 顺序、PID 标记/远程路径主要随 Run 生成 | 改为每 Case 唯一的工作目录、PGID、Checkpoint、环境、端口和结果；支持动态 Device IDs |
| \`ci_core/reporting.py\` 的 \`case_summary()\` 已解析 \`CASE_RESULT\` | 复用解析语义，改为 Case 级及时上报、独立失败、幂等重试，并保留汇总诊断 |
| \`ci_core/job_state.py\` 当前 Run 级 JSON 读改写 | 增加 Case 级持久化、原子写、恢复与版本化；防多线程/多进程写丢失 |
| \`ci_core/broadcast.py\` 依赖固定 \`allocation\`，\`end\` 文案声称环境资源已全部释放 | 按实际租约广播，绝不能在同一 Host 还有其他 Job 时误报“全机空闲” |
| \`ci_core/logs.py\` 路径白名单、20 行/128 KiB 上限 | 支持 Case 独立目录，继续保证白名单与上传预算 |
| \`ci_core/registry.py\` 的 \`resolve_request(nodes, ngpu)\` 对照静态 allocation | 改为按 Case 自身 \`nnodes/ngpu\` 验证，并对照通道允许的资源等级/池 |
| \`.github/workflows/*\` 只有一个等待 Job；Workflow 级并发组可能把不同 Run 串行化 | 动态 Matrix、\`fail-fast: false\`、每 Run 唯一并发组；跨 Run 互斥完全由 Dispatcher 控制 |
| \`lite-actions/github/\` 有 4 个 Workflow + 1 个 Waiter 与模型仓 Blob SHA 一致 | 删除副本，删除 \`scripts/sync_ci_mirror.py\`；迁移 \`test_workflow_contract.py\` / \`test_waiter.py\` 依赖 |
| \`run-unified-agent.sh\` 只有 \`flock\` 单实例保护，无开机自动恢复 | 保留单实例；对重启/恢复提供运行手册和可验证恢复流程，是否引入 systemd 单列后续决策 |

**基线证据**：8P [Run 38034002241](https://github.com/depeng1994/torchtitan-npu/actions/runs/38034002241) Muon/AdamW 双 PASS；16P [Run 37942444656](https://github.com/depeng1994/torchtitan-npu/actions/runs/37942444656) 历史 PASS；Dispatcher Smoke [Run 38025794748](https://github.com/depeng1994/torchtitan-npu/actions/runs/38025794748) PASS。这些仅代表当时提交；动态调度上线前必须重跑受影响路径。

## 2. V2 职责与核心契约（设计冻结项）

### 2.1 仓库与单一事实来源

- **TorchTitan-NPU**：\`.github/workflows/\`、\`.github/scripts/lite_actions/{prepare_request,wait_result}.py\`、\`tests/integration_tests/nightly_all_models_test/\`；唯一的 Workflow/测试源。CPU Runner 应可从可信提交的 \`build_test_list()\` 生成轻量化测试计划，不再从手写第二份 Registry JSON 读取。
- **Lite Actions**：\`config/{pools,pipelines}.json\`、\`ci_core/\`、\`unified_agent.py\`、执行日志/资源状态；不再存储 Workflow/Waiter 镜像。
- **跨仓契约**：定义版本化 \`plan.json\`、\`case-result\` Schema；固定输入输出，不靠字符串匹配 Job 标题推断测试身份。Lite 仓保留最小静态协议 Fixture，不复制生产 YAML。
- **可信边界**：模型仓 \`OverrideDefinitions.env_vars\` 独占 CANN、HF、Checkpoint、optimizer/config 等用例环境；Dispatcher 仅负责运行时拓扑/设备/进程/端口等资源绑定。若环境项与运行时端口冲突，须在模型仓声明明确的端口分配策略，不能无声覆盖固定模型值。

### 2.2 Workflow Plan / Matrix 数据模型

一次可信 \`workflow_dispatch\`：

1. \`prepare\` 检查 actor、repo、ref、case 选择与数量，获取确切 \`head_sha\`，调用**仅使用标准库可执行**的模型仓测试目录发现入口展开 \`suite\`；输出稳定排序、唯一 \`test_id\` 的测试计划。经源码检查，\`OverrideDefinitions\` 当前定义为标准库 \`dataclass\`，但仍须实测在 \`ubuntu-latest\` 上不依赖 NPU/torch。若不能独立执行，先拆出无运行时依赖的元数据模块，**不得**在 Lite 仓复制用例定义。
2. \`prepare\` 输出 GitHub Matrix JSON，同时上传绑定 \`repo/run_id/attempt/head_sha/pipeline/test_ids\` 的请求 Artifact（建议 Schema V2，包含 \`plan_digest\`）。Matrix 与 Artifact **必须由同一测试计划对象生成**，并进行一致性校验。
3. 每个 Matrix Job 只接受固定 \`test_id\`，运行原生 \`ubuntu-latest\` Waiter；不触碰 NPU。建议展示名 \`A3-8P / Muon\`、\`A3-8P / AdamW\`，\`strategy.fail-fast=false\`。
4. Dispatcher 只领取一次可信 Run，验证 Artifact 身份/Commit SHA/计划摘要，再展开为多个唯一 Case Task；绝不可因多个 Matrix Jobs 而下载/执行同一用例多次。
5. 初始授权边界继续是 \`workflow_dispatch + depeng1994 + repo 白名单 + master\`。仅白名单扩大后才可讨论 PR SHA 执行，不把“显示在 PR Checks”当作 V2 已实现能力。
6. 明确 \`rerun all\`、\`rerun failed jobs\`、\`rerun one job\` 的 \`run_attempt\` 规则：首次 V2 **只保证重新完整运行一个 Workflow Run**；单 Job/失败 Job 局部重跑必须检测到缺少对应 attempt 的可信 Artifact 后安全失败，并提示使用完整重跑；后续若支持部分重跑，要先设计计划重新签发、Case 身份和证据合并，禁止复用旧 PASS 冒充新 Attempt。

**默认 V2 数量策略**：每个 A3-8P Run 最多 4 个唯一 Case（当前实际只有 2 个），16P 最多 2 个，A5 仍为 1 且禁用；总体 Worker、并发 Host 容量由 Dispatcher 配置决定。不要将 \`Matrix max-parallel=4\` 当成全局 NPU 容量限制。

### 2.3 Pipeline 需求 / Pool 供给

建议 V2 \`pipelines.json\`（形态示例，非现在的运行配置）：

\`\`\`json
{
  "a3-8p": {
    "workflow": "a3-8p-lite-actions.yml",
    "pool": "a3-shared",
    "resources": {"nodes": 1, "devices_per_node": 8, "distinct_hosts": true},
    "runner": {"kind": "ssh"},
    "timeout_seconds": 7500,
    "enabled": true,
    "pipeline_id": "a3-8p"
  },
  "a3-16p": {
    "workflow": "a3-16p-lite-actions.yml",
    "pool": "a3-shared",
    "resources": {"nodes": 2, "devices_per_node": 8, "distinct_hosts": true},
    "runner": {"kind": "ssh"},
    "timeout_seconds": 7500,
    "enabled": true,
    "pipeline_id": "a3-16p"
  }
}
\`\`\`

- \`resources\` 是通道允许的资源形态/授权边界；实际 \`nnodes/ngpu\` 必须取自模型仓用例，并与资源授权一致，**不能由 GitHub 输入任意资源规模**。
- \`pools.json\` 的 \`device_ids\` 继续是合法全集；新增可选的 \`allocation_groups\`/拓扑约束及 Host 容量上限、禁用设备、运行时端口策略。
- A3 每台 16 Device，**先以历史可工作的具体组合为已知起点**；其他 8P 分组、同机两组并发以及不同 Host 的不同 16P 配对，都须通过拓扑与实机测试才能标记 \`verified\`。不要把 \`[0..7]\` 与 \`[8..15]\` 直接推断为全部合法组合。
- 多机 Case 必须分配到 **不同 Host**；不能用单机的两个 8P 组伪装 16P 双节点。
- A5 \`node_ip\` 未核实，整池禁用；配置 Schema 可校验但不得有隐式启用路径。
- 测试用例里的 \`MASTER_PORT\`、\`HCCL_IF_BASE_PORT\` 目前存在固定值，**这是同机并发的明确冲突风险**。在保持模型环境归属前提下，引入模型可声明的“需动态分配端口”契约；调度器统一租约分配端口/范围并仅通过显式允许的运行时覆写注入。未支持动态端口的 Case 必须按端口冲突域互斥，不得并发。

### 2.4 Scheduler / Lease / 并发策略

- Scheduler 采用 **Topology-aware First Fit + Age/Fairness**：依据候选 Host/合法组、需求和设备锁挑选；先找到满足全部节点的集合，再一次性获取设备锁与 Host/端口预算，失败释放已经获得的部分资源，不长期占用部分设备等待另一个 Host。
- 保留当前 \`resources.py\` 文件锁原语（排序获取、冲突回滚），增加 Task/Lease 层：\`task_id\`、分配设备、Host、端口、开始/心跳/退出证据、状态和版本化持久化。跨线程/进程状态写必须原子，重启需可恢复。
- **外部占用**：获取候选租约后，逐 Host 根据已核实的 \`npu-smi card-die\` 进程映射做只读校验；发现外部进程时释放候选并尝试其他组合或排队；格式异常 **fail-closed**，不可抢占、kill 或误报空闲。
- **未知残留**：锁释放 / Agent 崩溃并不证明训练进程退出。对于曾被任务占用而清理未确认的设备标记为 \`QUARANTINED\`，即便文件锁可重取也不调度；只可经验证进程组归属、清理和空闲确认后解封。
- **Host 预算**：除芯片数外设置可配置 \`max_jobs_per_host\`（**默认 1**）；同机双 8P 须完成 CPU RAM/Host-backed AdamW optimizer、IO、HCCL/端口的实机压力测试后，才允许提升为 2。两台 A3 的 4 并发是**实验目标**，不是上线默认。
- **公平性**：资源不足进入有界队列，按等待时长/跨机大任务进行防饥饿调度。不得因一直抢到单机空闲组而使等待的 16P 长期无机会执行；增加最大等待时间和明确失败原因。
- **取消/超时**：必须关联 GitHub Run/Case 状态并停止该任务拥有的进程组，校验远端真正退出；运行中的 GitHub Job 被取消或超时，不得任由 SSH 训练无限继续。不得影响同 Host 的其他并发任务。
- **输出隔离**：Run/Attempt/Test ID 唯一远端目录、Checkpoint/日志/TensorBoard、PID/PGID、任务标记及端口；禁止覆盖上一尝试产物。原有 \`SAFE_PATH\` 白名单相应更新并维持严格路径校验。
- **广播语义**：按 Case 占用设备广播；当前同 Host 存在其他租约时，单任务结束不能宣称“整机资源全部释放”，可显示本任务结束及仍在运行的其他 CI 状态；广播失败的处理策略须保留现有安全门槛。
- **并发执行**：运行中的 Run 可展开多个 Task；跨 Run 全局共享同一池。单实例 Agent 不变，Worker 数有限、有背压，任务入队/出队幂等。

### 2.5 GitHub 状态与传输契约

- 每个 Case 具有独立的 \`QUEUED → ALLOCATING → RUNNING → VERIFYING → REPORTING → PASS/FAIL\`，并有 \`NOT_RUN/CANCELLED/TIMED_OUT/INFRA_ERROR\` 终态。终态不可从 FAIL 改成 PASS，除非属于新的 Attempt。
- 第一版继续用**调度机 Python → GitHub REST Commit Comment**、Waiter 消费的出站通信机制；结果必须绑定 \`repo/run_id/attempt/sha/pipeline/test_id\` 与传输协议版本。结果不匹配一律拒绝。
- Reporter 在 **每个 Case 完成时** 发出结果而非整 Run 完成；GitHub Matrix Waiter 分别完成，互不阻塞。缺失结果/失败/超时明确非零退出，绝不以其他 Case PASS 推断成功。
- 每个 Case 结果幂等（同 Task 同 Attempt 不重复写；异常重试可重放并核验 payload）；API 分页、短暂故障、429/403、迟到的 Comment、轮询初始时间窗口和多 Run 共享 SHA 均纳入测试。
- 保留最多 20 行脱敏摘要、原始日志留执行机、有界读取/POST 大小约束；Case 数增多时测量 GitHub API 调用频率及 GitHub-hosted Waiter 并发占用。**不得把含 Token、SSH 地址或内部敏感路径的训练日志原样回传。**
- V2 原生 Matrix Job 的成功/失败由 Waiter 退出码控制；**不需要 GitHub App / Checks REST API** 即可获得独立 Job。对 PR 未合并 SHA 的 Checks 与合并门禁不属于本次承诺。

### 2.6 切换与兼容策略

- 新旧协议 **V1/V2 并行兼容一段迁移期**：先上线能识别 V2 但默认仍用 V1 的 Dispatcher，再切换模型仓 Workflow；不能先把生产 Workflow 改成 V2 却让旧 Agent 无法识别。
- 每条 Pipeline 增加可回滚的 \`protocol_version\` / \`scheduler_mode\` 控制，默认 \`v1/static\`，经过分阶段验收后再单独开启 \`v2/dynamic\`；回退不复用未清理的任务或设备。
- **已有进行中 Run 不热迁移**：仅在所有任务结束、设备释放确认后停机切换；切换失败恢复原代码和原配置/Workflow。
- GitHub 模型仓当前 \`master\` 受 GitCode 强制镜像影响：上游 GitCode 收录新 Workflow 前，不得把仅存在 GitHub 镜像上的 V2 视为持久上线；需确认 GitCode 主源、GitHub 镜像的版本与推送顺序。
- 从 Lite Actions 移除 \`github/\` 的同时，将 \`tests/test_workflow_contract.py\`、\`tests/test_waiter.py\` 移至合理归属或改为静态协议 Fixture，最后删除 \`scripts/sync_ci_mirror.py\`，避免直接删除导致现有单测失败。

## 3. 分阶段落地（每阶段必须记录可核验证据）

> 进展标记：\`[ ]\` 未开始；\`[~]\` 进行中（手动更新）；\`[x]\` 完成并有证据。不得仅凭代码合并标记“实机通过”。  
> **每阶段必填记录**：实施 Commit/PR、单测结果、Actions Run、实机 Host/设备范围（公开文档仅写脱敏别名）、失败/回退结论及验收人。

### M0｜冻结基线、检查表与演练环境

- [ ] 固定两个仓的实施起点 SHA，确认调度机没有未完成任务，备份 \`config\`、任务状态与日志索引；禁止随意清空 \`state/\`。
- [ ] 离线跑 Lite Actions 全部单测、\`unified_agent.py --validate\`、模型仓相关 CPU 测试；记录 PASS 数量和现有已知失败。
- [ ] 核对 GitHub/GitCode 同步计划、默认分支允许范围、部署唯一 Agent、真实路径/权限与回滚手册。
- [ ] 验证 A3 当前 \`npu-smi\` 的 0..15 Device ID 到物理卡/Die 的对应关系，建立**脱敏**的已验证/未验证拓扑表；只读、不结束外部进程。
- [ ] 在模型仓 CPU-only \`ubuntu-latest\` 原型验证无需 NPU 能否从 \`build_test_list()\` 导出 Matrix JSON。

**Gate M0**：两仓基线可重现；旧 8P/16P 证据保存；测试列表发现路径可行；所有未证实的拓扑和同机 2 Job 标为禁用。任何一项不满足，只补环境和方案，不切生产链路。

### M1｜清理双仓重复代码和稳定契约

- [ ] 规范 V2 plan/result JSON Schema、任务 ID、Attempt 与幂等规则，新增跨仓协议样例和版本兼容单测。
- [ ] 将 Workflow/Waiter 的测试移到模型仓或最小 Fixture/可选跨仓契约测试，保证 Lite 仓 CPU 单测无需依赖另一仓工作树。
- [ ] 删除 Lite Actions 的 \`github/\` 全部 5 个重复文件及 \`scripts/sync_ci_mirror.py\`；刷新 README 的唯一源码说明。
- [ ] Dispatcher 暂保持旧 V1 执行路径不变；不得因为清理镜像而改变远端启动参数。

**Gate M1**：Lite 单测全通过；模型仓 Workflow/Waiter 合约测试通过；GitHub 上实际 Workflow 未被删除；旧 Dispatcher Smoke 仍通过。若不通过，恢复镜像/单测修改即可回滚，不涉及 NPU 资源。

### M2｜V2 Plan + Case 级结果协议（先不开放并发）

- [ ] Prepare 由固定 SHA 可信模型用例展开 Matrix 和带 Digest 的 Artifact；校验顺序、空/重复 Case、资源越权与环境来源。
- [ ] Dispatcher 增加 V2 Plan 领取、Case Task ID、幂等持久化；旧 V1 仍可执行。
- [ ] Reporter/Waiter 实现单 Case 结果匹配和及时回传；覆盖 PASS/FAIL/NOT_RUN/超时/乱序/迟到/分页/权限失败/重复结果。
- [ ] 将 8P Workflow 切成动态 Matrix，但暂时 \`max-parallel=1\`，Dispatcher 使用原静态 allocation 串行跑；明确局部 rerun fail-closed 行为。
- [ ] 验证两个不同 Case 即使其中一个失败，\`fail-fast=false\` 也不会跳过另一个；单个 Case 不得跨 Attempt 取旧结果。

**Gate M2**：本地 Mock/CPU 测试全通过；GitHub 独立 Muon/AdamW Job 均能产生**可信独立状态**；8P 基线双 PASS（5 steps + TensorBoard + 退出码）；仍然未启用动态选卡/同机并发。若实机不通过，回退模型仓 Workflow 到 V1。

### M3｜动态选卡（限定已验证拓扑、单任务执行）

- [ ] \`pools.json\` 引入经验证的合法拓扑组和可用状态，\`pipelines.json\` 用 \`resources\` 替换静态 \`allocation\`；保留迁移兼容读取与强 Schema 校验。
- [ ] Scheduler 将 Case \`nnodes/ngpu\` 与通道授权和 Pool 匹配，原子选取**完整** Allocation；锁冲突/外部占用自动尝试其他合法候选。
- [ ] 动态 Allocation 全链路传给 \`remote_preflight\`、\`broadcast_job\`、SSH stage/launch/verify/cleanup、\`ASCEND_RT_VISIBLE_DEVICES\`，不得复用静态设备号。
- [ ] 离线测试：正常分配、所有设备占用、外部进程、错误映射、两个 Host 配对、单 Host 不得冒充多节点、部分锁失败释放、Host 断联、候选碎片化。
- [ ] 真实 8P 测试先选择**历史已验证**的 A3-4 设备组，再分批验证其他候选组；逐组确认当地 Rank 映射、设备利用率和清理状态。

**Gate M3**：同一 8P Case 在不少于两个**已确认合法**的设备组成功执行，并且输出/退出行为与基线一致；外部占卡不会被抢占；无法验证的新组继续禁用。V2 资源模式逐通道可回退为静态配置。

### M4｜任务隔离、故障恢复与多任务调度安全

- [ ] 增加 Device/Host/端口复合租约，Case 独立 PID/PGID、目录、Checkpoint、TensorBoard；校验 \`ASCEND_RT_VISIBLE_DEVICES\` 与本地 Rank。
- [ ] 为脚本所用 \`MASTER_PORT\`、\`HCCL_IF_BASE_PORT\` 等实现显式“运行时动态端口”策略；不支持动态端口的 Case 维持冲突域互斥，不能硬并发。
- [ ] Host 默认 1 Job；加入内存/IO/端口 Host 预算与 \`max_jobs_per_host\` 可控配置；Task Queue 与 Worker 有界。
- [ ] 落实取消/超时/SSH 失联、部分节点失败、Agent 重启、远端残留、上报失败重试、设备隔离和解封路径。
- [ ] 更正广播：只有对应设备经过实际验证后才报告“本任务释放”，同 Host 其他租约运行时不发“全机空闲”。
- [ ] CPU/故障注入：并发 50+ 逻辑请求、资源争用、时序随机化、kill Agent、断 GitHub API、断一端 SSH、取消一个 Job；证明不重复训练、不重复锁卡、不误杀其他 Job、不错误 PASS。

**Gate M4**：在**未允许同机双 Job**的保守容量下，多 Run 同时提交仍能排队、串行安全完成；取消和中断后残留被清理或隔离，未验证清理一律禁止复用设备。Gate 未通过不得打开 \`max_jobs_per_host=2\`。

### M5｜真实 Matrix 并发 / A3 容量逐档验证

- [ ] \`strategy.fail-fast=false\`，GitHub 每 Run \`max-parallel\` 受控，Workflow 并发组改成不阻挡独立 Run；Dispatcher 为**全局**唯一资源仲裁。
- [ ] **2 × 8P（跨 Host）**：A3-3、A3-4 各 1 个并发，连续完成 5 steps/TensorBoard，验证独立 PASS、时间重叠、互不抢设备。
- [ ] **同 Host 2 × 8P**：先确认第二个设备组单独可用，再实测同 Host 两任务并发。分别检查两进程组、Host-backed AdamW 内存峰值、HCCL 端口、带宽、数据与 IO；若有 OOM/冲突，保持每 Host 1 Job，不发布满载能力。
- [ ] **4 × 8P（两机满载）**：仅在两台 Host 均通过同机并发后，安排多个可信 Run（当前套件只有两个唯一 8P Case）完成四任务同一时段运行；逐一核对真实 5 steps、TensorBoard、Exit Code、设备归属、清理、GitHub 独立 Jobs。不得仅看 Workflow Success。
- [ ] 混部排队与公平性：4×8P 满载时提交 16P 排队、2×8P 退出后的完整资源配对、重试和无饥饿；并发在 \`MAX_WORKERS\`、Queue、GitHub hosted Waiter 成本和超时预算内。
- [ ] 统计单机串行/双机并发/四任务并发的实际耗时、CPU 内存峰值、失败率；有数据才更新默认 Host 配额。

**Gate M5**：能够证实的容量才启用。最小成功标准是 **两 Host 各 1 个 8P 并发**；四任务是附加硬件容量门槛，如果不通过，功能依然可按受验证的容量上线，但计划必须明确“4 并发未实现”、保留风险与原因。

### M6｜16P 跨机回归与 A5 禁用校验

- [ ] 动态分配两个不同 Host 的合法 8P 组，验证每机 \`NGPU=8\`、\`NNODES=2\`、EP=16、HCCL 小网地址/网卡、端口与全局 Rank 映射。
- [ ] 至少一次 **A3 16P、5 steps、TensorBoard 和完整 GPU/NPU 清理** 的新链路实机回归，并核对两个 Node 日志及 GitHub Case PASS；上一轮的历史 PASS 不足以替代本次调度器变更的验证。
- [ ] 测试多机分配失败不占半个集群；一端断联时任务整体失败且其余端训练可安全回收或隔离；16P 与剩余单机任务不得设备重叠。
- [ ] A5 64P 仅验证 Schema、禁用通道、资源需求解析和 \`node_ip\` 缺失的 fail-closed；**不得调度任何真实 A5 训练**。

**Gate M6**：新链路 16P 实机通过；A5 始终 disabled；资源隔离与多机清理失败场景均满足安全门槛。

### M7｜联调验收、文档收敛和生产切换

- [ ] 两仓 CPU 单测、Workflow 语法/Matrix 解析测试、端到端 Dispatcher Smoke 均通过。
- [ ] 在基线快照上验收 V1 回退操作：停止接新单、等当前任务退出、核对设备空闲、恢复 V1 Workflow/调度配置后能成功完成 Smoke。
- [ ] 按证据表汇总每个 Case：GitHub Run URL、实际代码 SHA、实际分配 Host/Device（公开文档脱敏）、训练 steps、TensorBoard、退出码、回传与资源回收状态。
- [ ] 检查两个仓的最终 README（部署/运行/排队/问题处理/目录结构）、取消和恢复 Runbook；删除过期镜像描述，保留协议 Fixture。
- [ ] GitCode 主源合入/同步方案核实后，先将 Dispatcher 的 V2 兼容代码部署到调度机，后切模型仓 Workflow；确认切换过程中无在途任务、无 GitCode 强制同步覆盖。
- [ ] 在 Lite Actions \`main\` + 模型仓 \`master\` 留存验收 Commit/Run 证据，并视合入策略通过 PR Review，而不是绕开 Review 直接放量。

**Gate M7**：V2/旧路可切换；最终容量宣传与实际证据一致；两个仓的版本和工作区状态可复现；安全审计和文档完成。

## 4. 必测矩阵（最小验收集合）

| 测试编号 | 测试类型 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| T01 | CPU | 可信 Suite 展开、Matrix/Artifact Digest 一致 | 相同 SHA/Case 唯一；无重复/越权 |
| T02 | CPU | 外部 actor、其他分支、不可信 PR、任意 Shell 输入 | 全部拒绝，不启动 SSH |
| T03 | CPU | Matrix Job 与 Plan 不一致、Attempt 不同、晚到旧 Comment | 拒绝旧/伪造结果 |
| T04 | CPU | 4 个逻辑 8P Task 获取互斥锁 | 绝不重复占同一设备 |
| T05 | CPU | 16P 原子锁，第二 Host 不足 | 不留部分占用；重排队 |
| T06 | CPU | \`npu-smi\` 外部占用/未知进程格式 | 跳过或隔离，不抢占 |
| T07 | CPU | 崩溃后锁自动释放但远端 PID 仍存在 | 不重新分配脏设备 |
| T08 | CPU | GitHub Job Cancel、超时、SSH/HCCL 失败 | 终态正确、清理自己的进程或隔离 |
| T09 | CPU | 并发上报、GitHub 重试/限流/分页、晚到旧结果 | 幂等、单 Case 正确、不超上传预算 |
| T10 | CPU | 同 Host 2 Job 的广播、日志路径、端口冲突 | 不误报全机空闲、不覆盖、不串用端口 |
| T11 | Actions | Dispatcher Smoke | PASS |
| T12 | A3 实机 | 8P Muon、AdamW 分别独立 Matrix Job | 各 5 steps + TB + 两个 Job 独立 PASS |
| T13 | A3 实机 | 两 Host 各 1 个 8P 并发 | 真实并发，独立 PASS |
| T14 | A3 实机 | 同 Host 两个 8P（条件门槛） | 若通过可升配额；若失败保守禁用 |
| T15 | A3 实机 | 四个 8P 并发（条件门槛） | 四个 Job 完整可核验，否则标为未通过 |
| T16 | A3 实机 | 双机 16P / 5 steps / TB | 新动态链路 PASS、两机清理完成 |
| T17 | 配置/CPU | A5 64P disabled、缺失有效拓扑 | 永不进行真实训练 |
| T18 | 切换 | V2 → V1 回滚 | 不重复跑、不留远端残留、Smoke PASS |

测试失败不应直接“重跑到绿”：先记录失败原因及原始 Run、检查是否硬件占用/程序错误；不使用人工 kill 未授权进程清理环境。

## 5. 风险台账（上线阻断项）

| 风险 | 严重性 | 控制措施 / 必须通过的门槛 |
| --- | --- | --- |
| 32 Device 逻辑容量与实际物理拓扑不匹配 | 高 | 实机核对 Device→Card/Die→Rank、合法 8P 组；未验证组禁用 |
| 同机双 8P 的 Host RAM、AdamW swap、IO 压力 | 高 | \`max_jobs_per_host=1\` 起步，测峰值后才提升，失败退回 1 |
| 多任务 HCCL/Master 固定端口冲突 | 高 | 动态端口租约与显式模型契约；无端口隔离就不并发 |
| 16P 部分节点拿到卡、另一节点资源不足 | 高 | 原子全量租约，失败不占半套资源 |
| GitHub Cancel 后远端训练继续跑 | 高 | Run/Job 状态关联、拥有者校验、取消与超时收敛 |
| 调度机崩溃导致 File Lock 消失、孤儿进程还在 | 高 | 状态账本、重启对账、未知设备隔离、只清理本任务 |
| 多线程更新单 JSON 导致状态丢失 | 高 | 原子写、序列化/锁、重启恢复测试 |
| Commit Comment 错配、过期/重复 PASS | 高 | SHA/Run/Attempt/Test ID 全字段验签式一致性检查与幂等 |
| GitHub \`rerun failed jobs\` 没有新 Attempt Artifact | 中高 | 初版拒绝局部重跑，需完整 Run；后续可单独实现 |
| 同 Host 并发广播误报“资源全释放” | 中高 | 以 Host 租约计数、设备范围为准 |
| Matrix 扩大 GitHub-hosted Runner 等待成本 | 中 | 有界并发/等待/超时、统计占用、必要时单独优化传输；不牺牲身份安全 |
| GitCode 定时同步覆盖 GitHub Workflow | 高 | 主源先合入/镜像链路演练，先部署兼容 Agent 再切 Workflow |
| 删除 \`github/\` 镜像导致单测导入失败 | 中 | 先迁移测试，再删除镜像，CPU Gate |
| A5 误启用/未验证 IP | 高 | 禁用双重门禁（配置 + Workflow），仅 CPU 测试 |
| V2 改写训练模型 env_vars / 优化器语义 | 高 | 模型仓仍为唯一所有者，比较最终 argv/env 与已通过基线 |

**停机/回退条件**：任何越权执行、错卡/重复占卡、误杀外部进程、假 PASS、未知残留重新分配、跨 Host HCCL 错配、GitCode 覆盖或模型语义漂移，均立即停止新增任务并回退对应 Pipeline；保留日志和运行状态做根因分析。

## 6. 上线顺序 / 验收记账模板

- **合入顺序**：Lite Actions 先合入向后兼容的协议/调度实现（V2 默认关闭）→ 模型仓再合入 Matrix/Plan/Waiter（必须先有兼容 Agent）→ 逐个 Pipeline 灰度 → 全量确认 → 清除 V1 过渡代码。涉及 GitCode 同步前必须验证不会覆盖模型仓 Workflow。
- **回滚顺序**：停止领取新任务 → 取消或等待本系统在途任务 → 验证设备、PGID、端口和远端目录状态 → 恢复旧 Workflow 与旧调度配置/Agent → Dispatcher Smoke → 恢复接单。**禁止运行时热回滚**。
- **发布策略**：调度机实际在跑的 Agent 版本、GitHub Workflow 版本、模型测试 SHA 要有可审计对应；模型仓 PR 合并本身不是“生产部署完成”。

每个里程碑完成后在本计划下更新一条记录：

| 里程碑 | 状态 | 实现提交 / PR | 测试与 Run 证据 | 结论 / 遗留风险 |
| --- | --- | --- | --- | --- |
| M0 基线 | 待执行 | — | — | — |
| M1 去重与契约 | 待执行 | — | — | — |
| M2 Matrix 单任务 | 待执行 | — | — | — |
| M3 动态选卡 | 待执行 | — | — | — |
| M4 并发安全 | 待执行 | — | — | — |
| M5 8P 并发容量 | 待执行 | — | — | — |
| M6 16P / A5 | 待执行 | — | — | — |
| M7 切换与收口 | 待执行 | — | — | — |

## 7. 实施前必须明确的现场事实（不会改变安全默认值）

这些是**硬件和环境验收数据**，不是架构设计中的自由发挥项。无法核实时继续采用保守默认并禁止对应能力：

1. A3-3/A3-4 的 16 个逻辑 Device ID 是否均对应可独立分配的 NPU 计算单元；\`[0..7]\` 和 \`[8..15]\` 是否可分别独立完成单机 8P。
2. 同 Host 两个 DeepSeek-V4 Flash 8P（尤其一个使用 AdamW Virtual Optimizer）是否有足够 Host RAM、I/O 与 HCCL 隔离；**未验收时每 Host 只运行 1 Job**。
3. CANN/HCCL 固定端口、torchrun rendezvous 端口及多进程环境变量的真实生效链路；未隔离就不放行同 Host 双 Job。
4. 16P 的不同设备组组合是否均受支持；**只允许逐组验证后扩容**，至少复现当前已知的双机组合。
5. GitCode 主仓能否在本次重构发布前收录对应 Workflow；否则禁止将 GitHub 镜像版本标记为已稳定上线。

最终交付标准：**两个仓库职责清楚、Matrix Case 独立可观测、动态调度不会抢卡、至少 2 个 8P 跨 Host 并发实机通过、16P 新链路通过、A5 禁用、旧路可回退**。**4×8P** 作为有条件的容量扩展，只有实测通过才宣称支持。

---

### 进展记录

- **2026-10-10**：完成两仓源码与风险设计复审；创建本重构计划；尚未改动运行代码/Workflow/资源配置，尚未执行实机验收。
