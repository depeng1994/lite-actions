# Lite Actions V2 重构计划：Phase 1 串行动态调度 → Phase 2 资源池并发

> **文档定位**：本文档为 Lite Actions V2 架构重构的技术设计契约与阶段验证台账（归档备忘）。
> - **日常使用、系统架构与首次部署**：请直接阅读 [README.md](README.md)；
> - **模型测试用例编写**：请查阅模型仓 [tests/integration_tests/README.md](https://github.com/depeng1994/torchtitan-npu/blob/master/tests/integration_tests/README.md)。

> **下一阶段设计**：[第 8 节：同 Node 双 8P／8P+16P／双 16P、HCCL 全端口租约与 64P>16P>8P 准入优先级](#8-phase-25-增量详细设计同-node-多任务双-16p-与大集群优先调度2026-10-12)，当前**仅设计、未实施**。
>
> **现行实施状态（2026-10-12）**：本文件保留 2026-10-10/11 的 Phase 1 串行架构历史设计与验收记录；下方历史规划表并非当前运行约束。Phase 2 的 **Case 级并发、设备/端口租约、跨 Host 2×8P** 已提交并部署（Lite `78015f9` / 模型 `e465e79`），Smoke、跨 Host 8P 并发、双机 16P 已实机 PASS。**Phase 2 整体尚未 Done**：同 Host 2×8P 容量与长期稳定性、8P/16P 同时混部、故障注入及串行回滚仍待单独验收。A5 保持禁用。
>
> **历史基线说明**：此前 Phase 1 全局串行已完成其当时的 GitHub/生产 Gate；当时 GitCode MR #985 等主源归一工作另行跟踪，不因本轮 CI 并发验证自动宣告完成。下文 Phase 1 的串行要求是历史契约，现行资源调度行为以 [README](README.md) 为准。
> 方案：**A（已确认）**。Phase 1 = 双仓解耦 + GitHub Matrix Jobs + 动态选卡 + **全局串行训练**；Phase 2 = 在 Phase 1 稳定基线之上增加**真正并发**。  
> 创建：2026-10-10；最近一次状态更新：2026-10-12。
> 计划初始代码基线：`lite-actions/main 2a0427c`、`torchtitan-npu/master b91257e`；本计划文件的维护提交不等于执行代码基线更新。  
> 关键证据：[8P 双 Case PASS（历史）](https://github.com/depeng1994/torchtitan-npu/actions/runs/38034002241) · [16P PASS（历史）](https://github.com/depeng1994/torchtitan-npu/actions/runs/37942444656) · [Dispatcher Smoke（历史）](https://github.com/depeng1994/torchtitan-npu/actions/runs/38025794748)。历史结果不可代替重构后的回归。

## 0. 两阶段目标与最终交付边界

| 维度 | Phase 1：基础架构与串行动态调度（**本轮实施**） | Phase 2：动态资源池并发（**第二轮实施**） |
| --- | --- | --- |
| 重构定位 | 架构闭环、正确性、兼容与可回退 | 并发吞吐、隔离、公平性与容量 |
| `lite-actions/github/` 镜像 | **删除**：模型仓是 Workflow/Waiter 唯一来源 | 不恢复镜像 |
| 测试定义 | `OverrideDefinitions/build_test_list()` 唯一模型测试来源 | 沿用 |
| GitHub 展示 | **动态 Matrix：每个 Test Case 独立 Job、独立 PASS/FAIL** | 沿用并允许真正同时 Running |
| 资源定义 | Pool 管理设备；Pipeline 声明池/通道允许的拓扑，不绑定固定 Host/Device | 沿用并添加 Host 级预算与端口租约 |
| 设备选择 | 根据合法拓扑、外部进程与可用设备**动态选卡**；8P/16P 均支持 | 多个 Case 可同时获得互不重叠的动态分配 |
| 调度执行 | **全局任意时刻最多 1 个执行中的训练 Case**（所有 Workflow/Run/Host 共享） | 根据租约、拓扑与 Host 预算并发；逐档从 2 到条件性的 4×8P |
| 队列 | 多 Case、多 Run 可以排队；前一 Case 完整退出并确认资源安全后再执行下一个 | 全局共享队列，支持资源饥饿防护与碎片化处理 |
| 外部占用策略 | 可信 `auto-occupy` / `vllm` 可按 PID 定点清理；其他用户进程等释放 | 同一策略，额外防止误伤其他并发 CI 任务 |
| 失败、超时、取消 | **必须具备**本任务进程清理/结果终态/脏设备隔离，不能推迟到 Phase 2 | 加强为多任务隔离、Agent 崩溃恢复与并发故障注入 |
| 通信端口 | 维持单训练任务运行；保留模型仓当前端口语义，验证不会被外部占用冲突 | 同 Host 多训练任务必须具有端口租约和实际生效的运行时覆写 |
| 8P / 16P | 两条 8P 5 steps + TensorBoard、1 条新链路 16P 5 steps + TensorBoard 必须通过 | 8P 并发、8P/16P 混部验证；16P 并发兼容不退化 |
| A5 64P | 继续禁用；仅离线配置/安全校验 | 继续禁用；上线另立专项验收 |
| 发布策略 | 完成后可以**独立上线**、保留动态选卡但串行 | 满足容量与隔离门槛后按 Host 分批放量 |

**不变的总体原则**：GitHub Actions 只负责安全入口、Matrix/Checks、等待；调度机 Python 领取任务、动态分配并通过 SSH 执行；执行机不持有 GitHub Token、不调用 AI Agent API；完整训练日志留内网，GitHub 最多返回经过筛选的 20 行诊断。**不**引入 Self-hosted Runner/Kubernetes，不开放不可信 PR/Fork 的任意代码，不自动启用 A5。

**重要区分**：Phase 1 的“Matrix 并行生成多个 GitHub Job”不等于“多个训练 Case 并行运行”。即使 GitHub 中多个 Waiter Job 看起来同时处于 in-progress，**调度机全局训练槽位仍严格为 1**。`strategy.max-parallel=1` 只限制某一个 Matrix 的等待 Job，**不足以实现跨 Workflow/Run 的全局串行**；必须由 Dispatcher 统一保证。

## 1. 两仓职责及现状审查

### 1.1 单一事实来源

| 仓库 | 正式所有权 |
| --- | --- |
| `depeng1994/torchtitan-npu`，以及 GitCode 上游主源 | `.github/workflows/`、`.github/scripts/lite_actions/{prepare_request,wait_result}.py`、模型测试 `tests/integration_tests/nightly_all_models_test/` 和 `OverrideDefinitions.env_vars` |
| `depeng1994/lite-actions` | `config/{pools,pipelines}.json`、`ci_core/`、`unified_agent.py`、资源仲裁/SSH/状态/报告、协议测试 |
| GitHub ↔ 调度机接口 | **版本化**任务清单/结果协议；固定 `repo + run_id + attempt + sha + pipeline + test_id`，不靠显示名称作为执行身份 |

- 模型仓独占 `ASCEND_SET_ENV_PATH`、`HF_ASSETS_PATH`、`CKPT_INIT_LOAD_PATH`、`CONFIG`、优化器、steps 与模型专属变量；不能把它们改成 Lite Actions 的第二套配置。
- Pool 是设备及网络的唯一权威配置；Pipeline 仅描述允许使用哪个 Pool、需要的节点/每节点设备数量、超时及开关。任务真实 `nnodes/ngpu` 来自模型仓可信 Case，必须与 Pipeline 授权规格相符。
- `lite-actions/github/` 当前 4 个 Workflow + 1 个 Waiter 是镜像副本；删除前先迁移 `tests/test_workflow_contract.py` / `tests/test_waiter.py` 的依赖，再删除 `scripts/sync_ci_mirror.py`。Lite Actions 可保留小型、固定版本的**协议 fixture**，但不能再复制生产 Workflow。
- 模型仓 `master` 目前可能被 GitCode 定时强制同步覆盖：切换生产 Workflow 前必须确认 GitCode 主源已经收录对应变更及同步顺序。

### 1.2 源码已核对的技术耦合

| 现状 | Phase 1 改造 | Phase 2 扩展 |
| --- | --- | --- |
| `pipelines.json` 固定 `allocation` | 改为资源规格；单任务动态产生 Allocation | 同时产生多份 Allocation |
| `resources.py` 通过 `fcntl.flock` 锁定 `(host_id, device_id)` | 复用互斥原语；增加候选组搜索、释放确认、持久化串行任务状态 | 同一 Pool 复合租约、Host 预算、锁冲突公平排队 |
| `unified_agent.py` `MAX_WORKERS=2`，Run 级领取 | 一个 Run 展开多个 Case Task；**全局 active training slot = 1**；Run/Case 双层幂等 | 多 Worker 训练，仍由全局 Scheduler 仲裁 |
| `ssh_runner.py` 一个 Run 中顺序遍历所有 Case | 一个 Case 一个执行单元；动态 Host/Device、独立目录/PID/结果 | 多 Case 真正同时执行；端口及 Host 资源隔离 |
| `reporting.py` 聚合式 `case_summary()` | 逐 Case 完成即上报；Matrix Waiter 独立结束 | 多 Case 并发上报，增加事务一致性与限流 |
| `job_state.py` Run 级 JSON | Task/Attempt 持久化、写入原子性、崩溃恢复/未知资源隔离 | 多线程/进程并发状态一致性 |
| `dispatch.py`、`broadcast.py`、`logs.py` 假定固定 allocation 和单目录 | 全链路读取运行时 Allocation、按本次任务设备/日志广播与取日志 | 同 Host 多任务不可误报“全机空闲”或串日志 |
| 16P 配置目前设置了固定 `MASTER_PORT` 和 `HCCL_IF_BASE_PORT` | **不改变已通过的单任务模型行为**，检查是否与外部占用冲突 | 同 Host 并发时必须提供模型授权的动态端口机制 |

## 2. 设计契约（Phase 1 必须建立、Phase 2 必须复用）

### 2.1 测试计划与 GitHub Matrix

1. `prepare` 校验 Actor/仓库/分支/Workflow、选择数上限和固定 SHA，读取模型仓可信 `build_test_list()`，将 Suite 展开为唯一 Test ID。**先用 ubuntu-latest CPU Runner 验证能否无 NPU 依赖运行元数据检查**；若失败，优先拆出仅使用标准库的元数据层，禁止在 Lite Actions 再维护一份用例注册表。
2. 从**同一份已展开计划**生成 GitHub Matrix Job 列表与身份绑定的 Artifact（`repo/run_id/attempt/sha/pipeline/cases/plan_digest/schema_version`），排序确定、拒绝重复用例和未经授权的资源规模。
3. GitHub Matrix 每项只有一个受信任 Test ID，独立 Job、`fail-fast: false`，只运行 `ubuntu-latest` Waiter。Waiter 永远不执行训练，仅接收**该 Case 对应**的 PASS/FAIL 等终态。现有 `master + workflow_dispatch + depeng1994` 白名单保持不变。
4. Dispatcher **每个 Run 仅领取一次**、核对 Artifact 与 SHA 后展开 Case Task；不能因为两个 Matrix Jobs 分别等待就将同一训练重复触发。Case Task 唯一 ID = `repository/run_id/attempt/pipeline/test_id`。
5. 第一个 Case 完成即可单独上报并结束其 Job，无需等另一个 Case；剩余 Case 根据 Phase 1 的全局串行槽位依次运行。失败时 `fail-fast=false`，确保符合计划的其他 Case 仍可执行或明确报告 NOT_RUN，而不是无声跳过。
6. **重跑策略**：Phase 1 首先只支持重新触发**完整 Workflow** 生成新 Run；对于 GitHub 原生 `re-run failed jobs` / 局部重跑等无相应新 Attempt Artifact 的情形必须 fail-closed，并在 Job 中解释原因；不得读取旧 Attempt PASS 冒充新结果。局部重跑支持需另行评审并更新契约。

### 2.2 动态资源选择与全局串行

`pools.json` 保留真实 `host_id`、SSH、`node_ip`、网卡、`device_ids`、`process_indexing`；增加**经实机核查的合法分组/拓扑**信息，默认只启用既有成功证据支持的组合。注意 A3 的 `card-die` 与物理设备映射，不要认为任意 8 个逻辑 ID 都能组成合法 8P。

`pipelines.json` 从固定 `allocation` 变更为授权规格，例如：

~~~json
{
  "a3-8p": {
    "workflow": "a3_8p_lite_actions.yml",
    "pool": "a3-shared",
    "resources": {"nodes": 1, "devices_per_node": 8},
    "runner": {"kind": "ssh"},
    "timeout_seconds": 7500,
    "enabled": true,
    "pipeline_id": "a3-8p"
  },
  "a3-16p": {
    "workflow": "a3_16p_lite_actions.yml",
    "pool": "a3-shared",
    "resources": {"nodes": 2, "devices_per_node": 8},
    "runner": {"kind": "ssh"},
    "timeout_seconds": 7500,
    "enabled": true,
    "pipeline_id": "a3-16p"
  }
}
~~~

以上仅为目标 Schema 示例，**不是运行中配置**。单个 Test Case 的 `ngpu=8, nnodes=2` 指两个不同 Host，每 Host 8 张，不得用同机两个 8P 分组凑成 16P。

Phase 1 Scheduler 流程：

~~~text
已授权 Case → QUEUED → 等待全局唯一训练槽位
→ 获取可信模型用例 nnodes/ngpu → 枚举“已验证合法”设备组/多机组合
→ 原子获取整组设备锁 → 通过 npu-smi/Host 只读检查外部占用
→ 必要时按授权类别定点清理 + 再次核验 / 改选组合 / 等待
→ STAGING → RUNNING → VERIFYING → 进程与设备释放确认
→ REPORTING → PASS / FAIL / NOT_RUN / TIMED_OUT / INFRA_ERROR
→ 释放全局训练槽位，再运行下一个 Case
~~~

- 阶段一**同一时刻只有一个 Case 处于 STAGING/RUNNING/VERIFYING/释放清理环节**；GitHub 同时提交多少个 Run 都不能突破此条件。
- 候选组不足或存在普通用户进程时，优先寻找其他合法组；全部不可用则进入有界等待，等待期间不长期占用部分设备锁，不主动停止其他用户进程；排队/资源超时要与模型训练失败区分。
- 原子锁必须一次获得全套单机/多机设备；获取部分锁失败则释放。锁定后再检查外部占用；进程表格式不认识或无法确认清理效果时 fail-closed。
- 即使是串行任务，仍需**独立 Task 状态与设备污损隔离**：Agent 重启导致文件锁释放不等于远端训练已结束；必须检查远端标识/进程/设备后才可复用。
- 16P 新链路必须重新做 5 steps + TensorBoard 实机回归（历史通过不代替动态选卡验证）。

### 2.3 外部设备占用策略（维护者已确认，Phase 1 实现）

| 占用分类 | 执行策略 |
| --- | --- |
| 可信的 `auto-occupy` 进程 | 允许按**已核实 PID**定点终止，重检 NPU 后继续 |
| 可信的 `vllm` 进程或确认属于该服务的 Worker | 允许按**已核实 PID**定点终止，重检 NPU 后继续 |
| 其他用户训练、调试、交互式进程 | **不 Kill**；优先改选合法空闲设备组，否则等待用户自行释放 |
| 不明 PID、宿主机/容器映射不清、进程已变更、其他 Lite Actions 活跃任务 | **不可 Kill**；等待/隔离，禁止误杀 |

实现要求：`npu-smi` 关联目标 Device/PID，再核对正确 PID 命名空间的 `/proc/<pid>/cmdline`、进程树、UID、启动时间和本系统租约；不能仅按模糊进程名作出清理决定。按目标 PID 发送 SIGTERM，限时未退出且复核 PID/启动时间相同后才可 SIGKILL；禁止 `pkill -f`、`killall` 和全机扫杀。**不处理看门狗**；若进程自动重生，有限重试后排队/隔离，不无限 kill。必须记录清理证据，并在重新执行 `npu-smi` 证明可用之后才启动训练。

**阶段划分提示**：可清理外部进程是**第一阶段选卡策略的一部分**；**多个并发 CI 任务互不影响**则是第二阶段新增验收，不影响第一阶段先交付。

### 2.4 状态、日志、失败安全与回滚

- Case 终态只能由绑定 `repo/run_id/attempt/sha/pipeline/test_id` 的结果确定；当前 Commit Comment + GitHub Waiter 方案可延用并改为每 Case 一条结果，保留单条有界 JSON、最多 20 行脱敏日志；不要让新 Matrix Job 在等其他 Case 时被误判 PASS。
- 明确设备分配、SSH 传输、退出码、TensorBoard、清理/释放和 GitHub 上报的对应关系。进程退出不等于设备已释放；设备未确认空闲时标记 QUARANTINED，不可派发后续 Case。
- GitHub Job Cancel / Run Cancel / Waiter 超时：Dispatcher 必须停止本次任务所属进程组、保留异常证据；如果清理不确定，隔离相关设备并阻止后续派发。不能把用户主动取消误报为训练 PASS。取消轮询与运行中清理必须进入 Phase 1 验收。
- Phase 1 的失败安全保障可以保持**单一调度实例 + 单活动 Case**、已有 `fcntl.flock`、有界任务状态持久化；**不提前引入**多任务端口租约、同机 Host RAM 算法及多执行进程之间的复杂公平性。
- Phase 1 / Phase 2 均需要新旧版本的受控切换：**先部署向后兼容的 Dispatcher，再启用模型仓新 Workflow**，旧 Run 不热迁移。若 GitCode 主源未收录 V2 Workflow，不能将 GitHub 镜像版本标记为稳定上线。

## 3. Phase 1 实施计划（本轮开发与验收）

> **Phase 1 的 Definition of Done**：仓库去重、Matrix 两个独立 Job、动态 8P/16P 选卡、**任何时刻至多一个训练 Case**、外部占用策略、安全取消/恢复、8P/16P 5 steps 实机 PASS、A5 继续禁用、可回滚。**不得要求 2 或 4 个 Case 并发才能交付 Phase 1**。

### P1.0｜冻结生产基线与可信环境

- [x] 固定 lite-actions/main 与 torchtitan-npu/master 实施 SHA，记录 GitCode → GitHub 定时同步风险；备份配置、状态、日志索引与旧 Workflow 版本。
- [x] 离线跑 Lite Actions 单测、`unified_agent.py --validate` 及模型仓相关 CPU 测试；记录原本通过/失败的测试数量，提供 SHA 和测试日志。
- [x] 只读验证 A3-3/A3-4 的 0–15 Device ID 映射、两机 SSH、HCCL IP/网卡与当前合法拓扑候选；未知组合先标为 disabled。
- [x] 在 `ubuntu-latest` 或等价干净 CPU 环境验证 `build_test_list()` → Matrix 展开没有 NPU/torch 运行时依赖。
- [x] 确认唯一运行的调度 Agent 与当前在途任务状态；本阶段不 kill 任何进程、不重启生产服务。

**Gate P1.0**：基线可重现，可信测试清单发现可行；已有 A3 拓扑证据、不可用组及回滚文件明确。

### P1.1｜双仓职责清理与 V2 协议

- [x] 编写版本化计划/结果 Schema、Case Task ID、Attempt、Plan Digest、唯一 Test ID 与失败/取消终态契约；补 CPU/Mock 跨仓协议测试。
- [x] **先迁移测试再删副本**：模型仓持有 Workflow/Waiter 合约测试；Lite 仓保留 Dispatcher 协议与白名单测试，必要时提供最小 fixture，不能依赖模型仓镜像目录。
- [x] 删除 Lite Actions 的 `github/` 全部 5 个重复文件、`scripts/sync_ci_mirror.py`，修正 README/测试内旧路径引用。
- [x] 保持正在生产使用的 V1 Workflow 与 Dispatcher 行为不变；移除镜像不意味着改变实际运行代码。

**Gate P1.1**：两仓 CPU/协议测试均通过；模型仓实际 Workflow 仍可用；Dispatcher Smoke PASS；无冗余 Workflow/Waiter 副本。

### P1.2｜Matrix Jobs + Case 级报告（仍固定分配且串行）

- [x] Prepare 从单一可信模型元数据展开 Matrix 与 Artifact；Job 名称可读，Case ID 与实际运行身份精确对应，失败时 `fail-fast=false`。
- [x] Dispatcher 只领取一次 Run，然后展开多个 Case Task；**全局单任务槽位=1**，覆盖来自不同 Run/Workflow 的任务；此时仍可在兼容模式使用原有静态 Allocation。
- [x] Reporter/Waiter 逐 Case 独立报告 PASS/FAIL/NOT_RUN，正确处理超时、乱序、重复回传、分页、延迟 Comment 与跨 Attempt 错配。
- [x] 明确 GitHub `rerun failed jobs`/局部重试在初版的拒绝规则及用户提示；测试不会错误复用历史 PASS。
- [x] 真实 8P Muon + AdamW **各 5 steps + TB + Exit Code 0**，GitHub 显示两个独立 Job；用可控失败或 Mock 验证一个 FAIL 不会把另一条 PASS 改错。

**Gate P1.2**：两个独立 GitHub Jobs、正确状态和串行执行证据；两个 8P Case 的全链路功能未退化。阶段失败可单独回滚 Matrix Workflow。

### P1.3｜动态选卡 + 可授权外部进程定点清理（全局仍串行）

- [x] Pool 只登记经验证的合法组；Pipeline 以 `nodes/devices_per_node` 声明资源规格，模型用例规格与授权边界严格相符；Legacy Schema 保留受控回退。
- [x] Scheduler 只在 A3-3/A3-4 的已验证候选组中动态选卡，支持 1×8P 和 2 Host×8P；单个 Case 的 Host/Device 原子锁定，不占半套多机资源。
- [x] 动态 Allocation 贯穿 preflight、广播、SSH、可见设备、TensorBoard/日志、清理和结果归档；Pipeline 不再固定 Device ID。
- [x] 外部占用遵守已确认策略：`auto-occupy`/`vllm` 仅按已核实宿主机 PID 定点清理，普通用户进程等待/换组，未知 PID 禁止终止；清理后必须重检。
- [x] CPU/进程级安全测试覆盖宿主机/容器 PID 命名空间、card-die 映射、未知占用 fail-closed、跨进程设备锁/全局槽、崩溃 quarantine 与 PID 定点清理；真实外部占用回收另有 Run 38060703524 的宿主机证据。
- [x] 同一 8P Case 已在至少两个经实机验证的合法分组运行；未验证设备组仍保持禁用。

**Gate P1.3**：动态选卡成功、全局仍只有一个训练进程组/Case；用户进程从未被清理；授权进程定点清理并重检通过；所有未验证的资源组保持禁用。

### P1.4｜串行故障恢复、8P/16P 回归

- [x] Case 使用独立 task key、目录和日志；最小持久状态为 `queued/running/pending_report/reported/blocked_recovery`，排队 Future 不冒充 running，已完成训练只重试结果上报而不重跑。
- [x] GitHub 取消、远端超时、SSH 失联、Agent 重启和 GitHub API 故障均按 fail-closed 处理；无法确认释放时 quarantine，不能抢占或清理普通用户进程。
- [x] 取消/重启故障注入与历史实机取消证据确认：下一 Case 只有在上一 Case 清理完成且设备可确认复用后才允许进入训练槽。
- [x] 用新动态调度的 8P Muon 和 AdamW 分别跑通 5 steps + TensorBoard；对照以前的优化器、4K、CANN 路径与最后 CLI 参数，禁止改变训练业务语义。证据：38054910957 / 38059258896。
- [x] 用新动态调度的**双机 16P 重新跑通 5 steps + TensorBoard**，检查每 Host 8P、EP16、NODE_IPS、HCCL、Rank/端口、设备释放；不能用以前的 Run 代替。证据：38054913344。
- [x] A5 保持 `enabled=false`，并由模型 Workflow `CI_CHANNEL_ENABLED=false` 双重 fail-closed；不做 A5 实机。
  
**Gate P1.4**：8P 双 Case + 16P 新链路成功；取消/异常不误杀外部用户、不假 PASS、不导致脏设备复用；A5 仍禁用。

### P1.5｜Phase 1 完整回归、生产灰度与交付

- [x] 正式发布 SHA 证据：Lite `68bd691` 全量测试 **85 passed**；模型 `546b655` 目标测试 **25 passed**；[正式 Smoke 38083634170](https://github.com/depeng1994/torchtitan-npu/actions/runs/38083634170)、[8P Muon/AdamW 38084585959](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084585959)、[双机16P 38084588613](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084588613) 均 SUCCESS；模型目标 pytest 不冒充全仓库测试。
- [x] 模型 V2 Workflow 已合入 GitHub `master`（PR #28、`2fe2623`；PR #29、`546b655`）；正式 8P Run 有 Prepare + Muon + AdamW 三个成功 Job，16P 独立 Job 成功，受信任 master-only admission 继续有效。
- [x] Dispatcher 的 V1/V2 兼容与生产受控升级已完成，唯一 Agent 运行于 Lite `68bd691` 生产目录；模型 V2 已合并到 GitHub `master`，实机验证基线为 `546b655`；队列误写 running 修复 `db0b001` 已部署。GitCode 主源 MR #985 合并及 Sync Upstream 重启属于下方独立未完成发布 Gate。
- [x] 回滚演练：停收新单 → 确认设备释放 → 切回 V1 Agent/Workflow → Smoke PASS → 恢复 V2；证据 38060151936，不热回滚。
- [x] 两仓 README、Phase 1 运维/取消/恢复手册及 `REFACTOR_PLAN.md` 已按 2026-10-11 自审刷新，并明确 CPU / 控制面 Smoke / NPU 实机证据层级。
- [ ] **冻结 Phase 1 双主源正式发布版本**；GitHub+调度机+实机已通过，仍须 GitCode [MR #985](https://gitcode.com/cann/torchtitan-npu/merge_requests/985) 经有权限维护者合并，验证 GitCode/GitHub master 归一和恢复安全同步，之后才能正式标记 P1.5 Done。Phase 2 不得提前开启并发。

**Gate P1.5（Phase 1 正式 Done）**：Case Matrix 独立检查 + 动态 8P/16P + 全局串行安全 + 外部进程政策 + 可回退，具备可复现实机证据；**无需并发通过**。

## 4. Phase 2 实施计划（Phase 1 完成后才启动）

> **进入条件**：Phase 1 已冻结并验收；独立创建 Phase 2 开发分支和新基线。Phase 2 只扩展并发/容量，不重新设计 Matrix/测试协议，也不恢复重复代码。

### P2.0｜并发可行性与资源预算

- [ ] 复查 A3-3/A3-4 的 8P 分组及两机合计 32 Device 的真实对应关系；`[0..7]` 与 `[8..15]` 仅在**实机证明**后才同时启用。
- [ ] 测量 8P Muon/AdamW Virtual Optimizer 的 Host RAM、CPU、IO、网络/HCCL、Checkpoint 资源峰值；默认 `max_jobs_per_host=1`，有证据再允许 2。
- [ ] 确认模型仓现有固定 `MASTER_PORT`、`HCCL_IF_BASE_PORT`、rendezvous 端口是否支持运行时覆写及并发端口范围管理；无隔离能力则禁止同 Host 双任务。

**Gate P2.0**：每 Host 可用 8P 分组、端口和资源容量均有测量；不满足时明确降低并发容量，不硬开 4 Job。

### P2.1｜全局资源租约及多任务调度

- [ ] 将全局训练槽位从 1 改为按 Pool/Host/设备容量配置的有界 Worker；**仍由单一全局 Scheduler** 负责跨 Run/Workflow 决策。
- [ ] 扩展原子设备锁为 Device + Host 预算 + 端口复合租约；多机任务不得预占一半设备等待其他 Host。
- [ ] 对并发任务分别维护独立的进程组/PGID、Checkpoint、日志、TensorBoard、端口、取消信号与租约；清理自己不能影响其他运行中的 Case。
- [ ] 实现等待队列优先级和防饥饿机制，避免持续 8P 小任务挤占 16P 多机任务；未知残留设备维持隔离。
- [ ] 校正广播文案：一份 Case 退出时不能宣称“整机已完全空闲”，其他租约保持不受影响。

**Gate P2.1**：至少 50 个模拟任务下无重复分配、无资源锁泄漏、无跨任务 PID/端口冲突；取消/失联一个任务不影响同 Host 其他任务。

### P2.2｜跨 Host 2×8P 并发（首个实机门槛）

- [ ] A3-3、A3-4 各分配 1 个 8P Case，确保训练时间真实重叠，5 steps/TB/Exit Code/Case Job 全部独立正确。
- [ ] 验证同时有多个 GitHub Run 时 `max-parallel` 与 Dispatcher 全局调度不冲突；GitHub Waiter 只有状态功能，不会执行重复训练。
- [ ] 外部用户进程占卡时继续等待/改选；真实并发下确认设备锁、任务隔离、失败/取消清理。

**Gate P2.2**：两 Host 各一个 8P 的**真实并发**完全通过。满足该门槛可以按 2 并发上限灰度上线；不能因尚未达到 4 并发否定已有功能。

### P2.3｜同机双 8P、三至四任务容量验证（有条件）

- [ ] 单独验证第二个 8P 分组，再在同一 Host 上真实启动 2×8P，检测 Host RAM（特别是 AdamW 虚拟优化器）、CPU、IO、HCCL、端口与性能；失败退回该 Host `max_jobs_per_host=1`。
- [ ] 两机合计 3×8P 验证；通过后才执行 4×8P 满载并发（32 Device）；当前模型仓 Suite 只有两个唯一 8P Case，必要时使用**多个可信 Run** 构造四任务，而不是伪造新的测试用例。
- [ ] 4×8P 验收需逐 Case 提供 5 steps、TensorBoard、Exit Code、GitHub Check、真实设备分配、运行时间重叠、外部进程与残留清理证据；不只是四个绿灯。
- [ ] 统计串行/2 并发/4 并发的总吞吐、Host 内存峰值、OOM/HCCL 失败情况。硬件条件不足时明确宣告**2 并发上限**，不得承诺 4 并发。

**Gate P2.3**：能证实的并发档位才启用；4×8P 属**条件性容量目标**，不是“不达 4 就全部失败”的上线门槛。

### P2.4｜8P/16P 混部、容错、运维与发布

- [ ] 16P 请求必须一次拿到两个不同 Host 的合法 8P 组；与单机 8P 混合运行时不重叠、不过度保留设备、不产生饥饿。
- [ ] 真实 16P 动态链路在并发调度开启后的回归至少一次，核对跨机 Rank/HCCL 和资源完整释放；一端失败时不会终止其他任务的独立设备进程。
- [ ] 故障注入：一 Job Cancel、SSH 断联、Agent 崩溃、PID 复用、租约残留、GitHub API 限流、普通用户占用与外部可清理进程自动重生；无误杀/假 PASS/越权资源分配。
- [ ] 完整回滚到 Phase 1 全局串行模式，验证不热切换不遗留脏设备；最终更新 README、测试/运行日志证据与计划状态。
- [ ] A5 继续禁用，64P 与多机拓扑扩展另列后续版本。

**Gate P2.4（Phase 2 正式 Done）**：2×8P 跨 Host 实机并发 + 16P 混部安全 + 取消/故障隔离 + 可退回 Phase 1；4×8P 能否开放由单独容量实测决定。

## 5. 全量验收测试矩阵

| ID | 阶段 | 测试 | 通过条件 |
| --- | --- | --- | --- |
| F01 | P1 | CPU-only Suite → Matrix/Artifact | 同一可信 SHA 的计划唯一、稳定，Digest 一致 |
| F02 | P1 | 不可信 actor/ref/repo/任意模块/未知用例 | Fail-closed，不执行 SSH |
| F03 | P1 | 两 Case Job/同一 Run 领取 | 仅生成两个唯一 Task，分别独立结果，没有重复训练 |
| F04 | P1 | 跨 Run/Workflow 同时提交 | **任意时刻最多 1 个真实训练 Case**；其余排队 |
| F05 | P1 | Test ID/Run/Attempt/SHA 错配、乱序/延迟 Comment | 不会读到其他 Case 或其他尝试的结果 |
| F06 | P1 | 动态 8P / 16P 原子锁定、候选不足 | 只选已核实拓扑，原子占用，部分失败无资源泄漏 |
| F07 | P1 | 外部 `auto-occupy` / `vllm` vs 普通用户进程 | 只清理可信前两类、按 PID、留证据、重检；普通用户等待/改选 |
| F08 | P1 | PID 复用/命名空间歧义/进程重生 | 不误杀；有限重试，必要时隔离 |
| F09 | P1 | GitHub Cancel、超时、SSH 异常、Agent 重启 | 仅清理本 Case，未知残留隔离，不错误 PASS/继续占卡 |
| F10 | P1 | A3 8P Muon、AdamW | 各 5 steps + TensorBoard + 独立 GitHub Job PASS |
| F11 | P1 | A3 16P 双机 | 新动态选卡、EP16、两机 5 steps + TB、释放确认 |
| F12 | P1 | A5 配置 | disabled，未核实 HCCL/资源时绝不执行 |
| F13 | P1 | 删除镜像与回退 | Lite 仓无重复 Workflow/Waiter，测试通过，V1 Smoke 可恢复 |
| C01 | P2 | 50+ 模拟任务并发竞争 | 不双重分配，无锁/租约泄漏与其他 Task 误清理 |
| C02 | P2 | 跨 Host 两个 8P Job | 真实重叠、双 PASS、两台设备释放 |
| C03 | P2 | 同机两个 8P Job | 资源预算/端口隔离成功，才允许 Host 配额提升 |
| C04 | P2 | 三至四个 8P Job | 条件容量达标，4 Case 各有训练/Check/隔离证据 |
| C05 | P2 | 8P/16P 混部 | 双机原子分配、队列公平、错误隔离 |
| C06 | P2 | 单任务故障、Agent 崩溃、API 限流 | 其他任务不受影响，恢复安全，回退到串行可行 |

所有证据记录 **Commit SHA、Run URL、Host/分配 Device（对外脱敏）、Steps、TB、Exit Code、最终 Job 状态、清理结果**。某个 Case 曾经成功不代表最新改动成功；不以连续“重跑到绿”代替错误定位。

## 6. 风险控制与发布顺序

| 关键风险 | Phase 1 控制 | Phase 2 补充 |
| --- | --- | --- |
| Matrix 不等于并发；多个 Workflow 造成意外并发 | Dispatcher 全局 active slot=1；跨 Run 强制串行 | 槽位扩为全局、可计量租约 |
| 设备物理 ID / `card-die` / HCCL 拓扑不一致 | 已验证的分组白名单；单机/多机原子选卡 | 同 Host 两组可并发需额外实机证据 |
| 外部占用处理误杀 | 指定 `auto-occupy` / `vllm` 的目标 PID 定点清理；普通用户等待；确认 PID 启动时间与命名空间 | 并发租约所有者/进程组保护 |
| 取消或崩溃后释放文件锁但训练仍运行 | 清理证据、任务状态账本、QUARANTINED | 同 Host 多任务间独立隔离/恢复 |
| 同机端口/Host 内存冲突 | 全局串行，不修改模型已通过的端口配置 | 端口租约、Host 内存/IO/CPU 预算 |
| GitHub Matrix Waiter 产生旧 PASS / 假 PASS | 每 Case/Attempt 严格绑定，V1/V2 兼容期测试 | 并发分页/限流/超时压力测试 |
| GitHub 局部重跑缺乏新的 Artifact | Phase 1 明确拒绝局部重跑，提示完整启动新 Workflow | 若需支持，另立设计门槛 |
| GitCode 定时覆盖新的 GitHub Workflow | 核对主源先后顺序，先部署兼容 Dispatcher，再切 Workflow | 维持与 Phase 1 一致的策略 |
| A5 未实际验收却被启用 | 配置与 Workflow 双重禁用、无 HCCL IP 直接拒绝 | 仍保持禁用 |

**阻断/回滚触发条件**：越权执行、错误分配/设备重叠、误杀普通用户或其他 CI Task、旧 PASS 冒充新成功、未知远端残留被重用、16P HCCL 错配、GitCode 覆盖、模型优化器/环境语义漂移。一旦出现，暂停领取新任务、保留状态与日志，禁止在远端仍运行时热切换或清空 `state/`。

**先后顺序**：

1. Phase 1：Lite Actions 先支持 V2 但默认旧模式；模型仓随后合入 Prepare/Matrix/Waiter；静态串行验证后启用动态选卡；8P/16P 新链路通过，再完成 Phase 1 发布。
2. Phase 2：以 Phase 1 的成功代码为不可变基线，逐档放开并发；**默认 max_jobs_per_host=1**，实测允许后才增至 2。
3. 回滚：停止接单 → 等待/安全清理本系统在途任务 → 检查远端进程、设备、端口和日志 → 恢复上一阶段配置/代码 → Smoke 验证 → 恢复接单。

## 7. 进度跟踪与阶段验收台账

**更新规则**：任务用 `[ ]` / `[~]` / `[x]` 标记；里程碑只有在满足对应 Gate 且证据已记录时才允许标记 Done。每次更新必须附实施 SHA / PR、测试日志/Run URL、未解决风险；**不得将修改计划的 Commit 当成代码验收证据**。

| 里程碑 | 当前状态 | 代码 PR / SHA | 验收 Run / 日志 | 风险与结论 |
| --- | --- | --- | --- | --- |
| P1.0 基线/拓扑 | 已验收 | Lite `2a0427c` / 模型 `b91257e` | 基线 69 passed/1 skipped；CPU-only Prepare/Matrix 新链路已验证 | 已冻结 V1 原目录与回滚副本，合法资源组见下文 |
| P1.1 仓库去重/契约 | 已验收 | Lite PR #1 (`7824a83`)、模型 PR #28 (`99cd7e3`) | Lite `77 passed / 1 skipped`，模型 `24 passed`，V1 `60 passed` | 模型仓唯一 Workflow/Waiter，Lite 无生产镜像 |
| P1.2 Matrix/独立结果 | 已验收 | 模型 `4233e7d` | [8P Run 38050639931](https://github.com/depeng1994/torchtitan-npu/actions/runs/38050639931) | 两独立 Job、Muon/AdamW PASS rc0，串行上报 |
| P1.3 动态选卡/占用策略 | 已验收 | Lite `92b9b04` | [A3-4 8P Run 38050639931](https://github.com/depeng1994/torchtitan-npu/actions/runs/38050639931)、[A3-3 8P Run 38060703524](https://github.com/depeng1994/torchtitan-npu/actions/runs/38060703524)，77 passed/1 skipped | 同一 Muon 用例在 A3-4:8–15 与 A3-3:0–7 均 5 steps+TB PASS；A3-3 auto-occupy Worker 宿主机 PID 分类后定点清理并经 npu-smi 确认释放；未误杀其他进程；临时选择优先级已恢复 |
| P1.4 8P/16P 与恢复 | 已验收实机 Gate | 模型 `99cd7e3` / Lite `92b9b04` | [Muon Run 38054910957](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054910957)、[AdamW Run 38059258896](https://github.com/depeng1994/torchtitan-npu/actions/runs/38059258896)、[16P Run 38054913344](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054913344)、[Cancel Run 38054570924](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054570924) | 8P Muon/AdamW、双机16P 均 5 steps + TB + rc0；全局不重叠；取消远端子进程清理验证；前一次 AdamW 外部 SIGKILL 根因未证实，FAIL 保留不可擦除 |
| P1.5 Phase 1 发布 | **GitHub / 生产调度及最新实机 Gate 完成；GitCode MR 待合并** | Final-review baseline：Lite repo `main=fbb0631` / production `68bd691`；模型 repo `master=f748ddc` / runtime-verified `546b655`；GitCode `master=2994912`，待 [MR #985](https://gitcode.com/cann/torchtitan-npu/merge_requests/985) | [正式 Smoke 38083634170](https://github.com/depeng1994/torchtitan-npu/actions/runs/38083634170) PASS；正式 [16P 38084588613](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084588613)、[8P 38084585959](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084585959) 全部 GitHub SUCCESS、5 steps/TB/rc0 | 四条 SSH relay 已恢复；A3-4 /data 与容器 Overlay 0B，模型 PR #29 将 HF_HOME/HF_DATASETS_CACHE 转至 /mnt/share。runtime-verified SHA 的 8P Muon、AdamW 及双机16P 全部通过，全局串行、无 quarantine。GitCode 禁止作者自合并且 token 无 MERGE 权限；Sync Upstream 暂禁用，P1.5 尚未 Done |
| P2.0 并发资源预算 | **部分完成** | Lite `78015f9` | 3 个训练 Worker + 1 个 Smoke Worker、可用拓扑动态选组 | 已实现设备/端口租约与 Host 选择；CPU/内存/IO 峰值与容量预算仍待测 |
| P2.1 租约/多任务调度 | **已完成当前上线范围** | Lite `8a737e8` → `78015f9` / 模型 `e465e79` | Lite 103 passed/1 skipped、模型 31 passed；跨进程设备/端口锁与 Matrix Case 独立任务 | 当前 3 训练 Worker；并发恢复/故障注入需补充，不将完整 P2.1 Gate 宣称通过 |
| P2.2 跨 Host 2×8P | **实机验收通过** | Lite `78015f9` / 模型 `e465e79` | [8P 38147139071，Attempt 3](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139071) | A3-3 0–7 AdamW 与 A3-4 0–7 Muon 并发，均 5 steps、rc0、单独上报 PASS |
| P2.3 同机/4×8P 容量 | **尚未完成容量 Gate** | — | 8P [Run 38147139071](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139071) 曾完成同机不同设备组 Case | 仍缺 Host 内存/IO/HCCL 压力、长期稳定与 4×8P 负载数据，不标记 Done |
| P2.4 混部/发布 | **部分完成** | Lite `78015f9` / 模型 `e465e79` | [16P 38147139067，Attempt 2](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139067) PASS；[Smoke 38147139076](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139076) PASS | 生产已切换且 16P 双机回归通过；**8P 与 16P 同时运行、故障注入及回滚 Gate 未完成** |
| P2.5 同 Node 并发与优先级扩展 | **设计完成，实施待开始** | 仅第 8 节详细设计（代码未修改） | 计划 N01–N13：拓扑、Host/NPU 端口、双 16P、8P+16P、64P>16P>8P 优先准入 | 未验证的 A3-3 第二组与新 16P Bundle 均保持禁用；A5 保持禁用 |

### 2026-10-11 并发能力真实验收（当前发布基线）

| 范围 | 运行版本与证据 | 事实与结论 |
| --- | --- | --- |
| 两个 8P Matrix Case 跨 Host 同时训练 | Lite `78015f9` / 模型 `e465e79`，[Run 38147139071 Attempt 3](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139071) | 约 22:44:47 同时启动，A3-3 0–7 AdamW、A3-4 0–7 Muon；两者均 `NODE_EXIT=0`、`CASE_RESULT PASS 0`，完成时间约 22:52 |
| 两节点 16P 完整回归 | 同版本，[Run 38147139067 Attempt 2](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139067) | A3-3 0–7 与 A3-4 8–15，两个 `NODE_EXIT=0`，`CASE_RESULT PASS 0` |
| Smoke 与部署检查 | 同版本，[Run 38147139076](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/38147139076) | GitHub SUCCESS，生产 Dispatcher 已切换，最终无在途 Case、无 quarantine |

**验收边界**：上述为 *成功的对应 Attempt*，保留历史 `INFRA_ERROR`、`CANCELLED` 事实；跨 Host 并发和 16P 回归已通过，不能推断完成同 Host 全容量、同时混部、混合故障注入与回滚验证。本次仅文档刷新，不触发训练、不修改生产运行进程。

### 2026-10-10 Phase 1 实机回归与回滚证据

| 验证 | 模型 SHA / Lite SHA | Run 或现场 | 结果 |
| --- | --- | --- | --- |
| V2 8P Muon + Virtual AdamW 两独立 Matrix Jobs | `4233e7d` / `480a108` | [38050639931](https://github.com/depeng1994/torchtitan-npu/actions/runs/38050639931) | 各 5 steps、TensorBoard、rc=0，两个 Job PASS、全局串行 |
| GitCode/GitHub 归一的 16P 双机 | `99cd7e3` / `60e0d84` | [38054913344](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054913344) | A3-3:0–7+A3-4:8–15、双机 EP16、5 steps/TB、rc0、PASS |
| 归一版本 8P Muon | `99cd7e3` / `60e0d84` | [38054910957](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054910957) | Muon 5 steps PASS；同 Run AdamW 遇外部 SIGKILL（总体 FAIL，不隐去） |
| 归一版本 8P AdamW 独立重验 | `99cd7e3` / `7824a83` | [38059258896](https://github.com/depeng1994/torchtitan-npu/actions/runs/38059258896) | 5 steps、TB 事件与 Case PASS、rc0，Github Run success |
| 取消故障注入与远端清理修复 | `4233e7d` / `60e0d84` | [38053832324](https://github.com/depeng1994/torchtitan-npu/actions/runs/38053832324)、[38054570924](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054570924) | 复现跨 PGID rank 残留；改为 marker + PID 启动时间定点回收；后次 CANCELLED 且无残留、用户进程未受影响 |
| V2 兼容现有 V1 GitHub Master Workflow | 模型 `b91257e` / V2 Lite | [38059669234](https://github.com/depeng1994/torchtitan-npu/actions/runs/38059669234) | 兼容 Smoke PASS |
| 恢复到实际 V1 Agent + Workflow | 模型 `b91257e` / V1 Lite `2a0427c` | [38060151936](https://github.com/depeng1994/torchtitan-npu/actions/runs/38060151936) | 停 V2、启动唯一 V1 Agent、真实 Github Smoke PASS；停 V1 后恢复唯一 V2 Agent；V1 原状态/日志和 Pool 均已备份 |
| 第二合法 8P 组与真实外部占用回收 | `99cd7e3` / `92b9b04` | [38060703524](https://github.com/depeng1994/torchtitan-npu/actions/runs/38060703524) | A3-3:0–7 Muon 5 steps/TB rc0 GitHub PASS；确认 auto-occupy Worker PID 3598060，经 PID 类别+启动时间核实后只对目标回收，npu-smi 复核空闲，未改 watchdog；临时优先级恢复原值 |
| 正式 master + NFS HF 缓存双机 16P | 模型 546b655 / Lite 68bd691 | [Run 38084588613](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084588613) | 独立 16P Matrix Job GitHub success、Case PASS、A3-3:0–7+A3-4:8–15、5 steps/TensorBoard、rc0，资源释放无 quarantine；新缓存在两节点任务进程环境中验证。 |
| 正式 master + NFS HF 缓存 8P Muon/AdamW | 模型 546b655 / Lite 68bd691 | [Run 38084585959](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084585959) | Prepare、Muon、AdamW 三个 GitHub Jobs 均 SUCCESS；两个 Case 各 5 steps/TensorBoard/rc0/报告 PASS；A3-4:8–15 严格串行执行，无 quarantine。 |
| A5 64P 禁用与安全测试 | Lite `92b9b04` | `python3 -m pytest -q tests` 77 passed,1 skipped；模型 24 passed | `config/pipelines.json.enabled=false`、GitHub Workflow CI_CHANNEL_ENABLED=false；A5 未运行 |

**保留的异常事实**：归一 SHA `99cd7e3` 在 [38054910957](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054910957) 的 8P AdamW 第 1 step 后遭 SIGKILL（rc137）；未找到 OOM 证据，不能断言根因或删除 FAIL。后续同 SHA 的独立 8P AdamW 回归 5 steps/TB PASS；此前隔离通过 Host npu-smi 与完整任务 marker 查空后才人工解除，保留审计信息。生产默认不自动 kill 外部进程；只有获维护者授权且精确识别的 auto-occupy/vllm 才能按宿主机 PID 回收。

### 2026-10-11 Phase 1 独立自审证据分层

| 证据层级 | 版本 / 证据 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| CPU/进程级回归 | Lite `9c8423d`: `python3 -m pytest -q tests` **84 passed**；模型 `3bb3924`: 目标 pytest **25 passed** | V1 崩溃状态 fail-closed、Agent 单例/全局训练槽的真实跨进程互斥、评论分页幂等、A5 Workflow 禁用契约及 V2 协议 | 不证明新 Lite SHA 已跑过真实 NPU 训练 |
| 生产控制面 Smoke | [38061822173](https://github.com/depeng1994/torchtitan-npu/actions/runs/38061822173)，模型 `master` `b91257e` + 已发布 Lite V2 Dispatcher | PR #1 合并后的生产 Dispatcher 能兼容 V1 master Workflow 并完成 GitHub 端到端 Smoke | 不是 8P/16P NPU 训练，也不覆盖随后 `9c8423d` 自审修复 |
| A3 实机训练历史证据 | 8P [38050639931](https://github.com/depeng1994/torchtitan-npu/actions/runs/38050639931)、AdamW [38059258896](https://github.com/depeng1994/torchtitan-npu/actions/runs/38059258896)、16P [38054913344](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054913344)、取消 [38054570924](https://github.com/depeng1994/torchtitan-npu/actions/runs/38054570924) | V2 Matrix、真实 5-step/TB、动态 8P/16P 与取消清理在对应历史 SHA 上通过 | 不是 `9c8423d` / `3bb3924` 的新实机证据 |
| A5 | `enabled=false`，模型 Workflow admission=false | 未经验证不会执行 | **无** A5 实机通过证据 |

自审后仍遵守“不热部署”规则：`9c8423d` 合入源码不等于生产进程已加载该代码。上线该安全修复必须在无在途任务且设备释放已验证后，按 runbook 停止旧 Agent、更新代码、启动唯一 Agent，再做 Smoke；本次自审未清理任何外部进程、未修改生产 Pool/白名单。

### 2026-10-11 最新正式发布验收与剩余 Gate

- **生产实际版本与仓库 HEAD**：Lite Actions 生产 checkout 为 `68bd691`，调度机唯一 Agent PID `2133238`（`flock` 包装 PID `2133232`）、cwd 为生产 Lite 目录；本轮 final review 的 Lite repo baseline 为 `main=fbb0631`，相对生产仅有本计划/runbook 文档提交；本闭环分支继续只改文档。模型实机验证基线为 `546b655`（PR #28 与 HF 共享缓存修复 PR #29 均已合入），本轮模型 repo baseline 为 `master=f748ddc`，相对该基线仅有 integration README 文档提交；A3 8P/16P 正式 Workflow 均启用。`A3_CI_ALLOWED_BRANCHES=master`、`A3_CI_RECLAIM_EXTERNAL_PROCESSES=0`。Lite `python3 -m pytest -q tests` **85 passed**，模型目标 pytest **25 passed**。
- **runtime-verified SHA 的硬件证据**：[8P 38084585959](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084585959)，Muon + AdamW 两个 Case 均 5 steps、rc0、独立 GitHub Job/Commit Comment PASS；[16P 38084588613](https://github.com/depeng1994/torchtitan-npu/actions/runs/38084588613)，A3-3:0–7 + A3-4:8–15、5 steps、rc0、Case/Job PASS。Dispatcher `unified-agent.log` 04:40–05:04 +08:00 的三次 `allocated Case`、广播成功与三个 `V2 Case reported ... PASS` 证明从 16P 到 Muon 再到 AdamW **无训练重叠**，状态台账 83/83 `reported`、无 quarantine。TensorBoard 检查由模型 Case 运行逻辑记录；不可将 GitHub 绿灯单独视作训练证据。
- **故障历史保留**：较早的 [38082962268](https://github.com/depeng1994/torchtitan-npu/actions/runs/38082962268) INFRA_ERROR、[38082959566](https://github.com/depeng1994/torchtitan-npu/actions/runs/38082959566) CANCELLED（当时 relay 中断）以及 AdamW SIGKILL rc137 的历史故障仍保留，不因本次新 PASS 删除。
- **仅存发布阻断**：GitCode `master=2994912`，尚不包含上述 GitHub V2 master。已提交受保护分支 [GitCode MR #985](https://gitcode.com/cann/torchtitan-npu/merge_requests/985)，该仓不允许当前作者自行合并，需另一位具备 MERGE 权限的维护者合入。`Sync Upstream` 当前 `disabled_manually`，不得提前重开，否则 GitHub `master` 会被每日强制镜像覆盖。MR 合并后需按实际合并 SHA 与最新 GitHub 变更做双 `master` 对账，再恢复受控同步并验证 Workflow 完整性。**PR #4 源码/审查意见可闭环，但双主源发布 P1.5 不可提前标记 Done。**

### 尚需实机核实的事实（不阻塞方案冻结，但阻塞相应功能放量）

- A3-3 的 8–15 仍未实机验收为合法 8P 组；A3-3 0–7、A3-4 0–7/8–15 已有 P1 证据。P1 只启用已确认组，同 Host 双任务容量仍属于 Phase 2。
- `auto-occupy` / `vllm` 在宿主机与训练容器里的真实 PID 映射、进程树及重生行为；未核实类别不得执行 kill。
- 已验收的 16P 组合 A3-3 0–7 + A3-4 8–15 已通过 HCCL/Rank 实机；其他实验组合、网卡或地址变化仍须重新实机验收，不能外推。
- 同 Host 两个 8P 的 Host 内存峰值（特别是 AdamW Virtual Optimizer）、HCCL/torchrun 端口与 IO；**仅 Phase 2** 开启同机双任务。
- GitCode → GitHub 主源与强制同步的切换窗口，未妥善处理时不标记生产发布完成。

### 变更记录

- **2026-10-10**：原始单阶段重构计划建立，核查已有两仓源码与历史 8P/16P 验收。
- **2026-10-10**：依维护者要求：`auto-occupy`、`vllm` 可按 PID 定点清理；普通用户进程等待释放，并增加防误杀/重检门槛。
- **2026-10-10**：维护者确认**方案 A**：Phase 1 完成 Matrix + 动态选卡但**不并发**；Phase 2 再做多任务并发。重新编排 P1.0–P1.5、P2.0–P2.4 的目标、门槛、测试矩阵、发布与回滚；**仅更新计划，未改运行代码、未触发训练、未结束进程**。
- **2026-10-10**：启动 Phase 1 方案 A 开发。在调度机**独立 Git worktree**建立 `refactor/v2-phase1-serial` 双仓分支；完成 V2 Plan/Matrix/Case Waiter、资源配置解耦及串行动态选卡初版，删除 Lite 重复 `github/` 镜像；推送 [Lite Actions Draft PR #1](https://github.com/depeng1994/lite-actions/pull/1)、[TorchTitan-NPU Draft PR #28](https://github.com/depeng1994/torchtitan-npu/pull/28)。Lite CPU unittest 67 OK（1 skipped），模型仓相关 pytest 20 passed；A3 做了只读 SSH/NPU 探测。**尚未在新链路触发真实 NPU 训练、Kill 外部进程、切换生产 Agent 或完成 Phase 1 门禁。**
- **2026-10-10**：Phase 1 安全/云端验证追加：Lite 单测 **69 项 OK（1 skipped）**，模型仓 pytest **21 passed**；[GitHub Run 38042640944](https://github.com/depeng1994/torchtitan-npu/actions/runs/38042640944) **Prepare 成功并生成 Muon/AdamW 两个独立 Matrix Jobs**，为避免特性分支任务被生产 Agent 领取，主动取消等待 Job，**未跑实际 NPU**；临时特性分支准入已撤销，恢复 master-only。只读定位了 A3 `npu-smi` 宿主机 PID 与容器 PID 命名空间差异，外部授权进程清理已改为宿主机 SSH 定点操作，并新增防误杀单测；**尚未在物理机执行任何进程清理**。P1.3/P1.4 的实机门禁仍未通过。
- **2026-10-10 18:57 +08:00**：**第一轮 Phase 1 实机验收窗口已结束，旧生产 Dispatcher 已恢复**。切换前 GitHub 无在途 workflow_dispatch，备份了旧配置/状态；切换期间使用单 Worker + 外部进程清理关闭。8P [Run 38043726171](https://github.com/depeng1994/torchtitan-npu/actions/runs/38043726171)：Muon 5 steps/TB/Exit 0，因结束 wall 失败被错误上报 FAIL；AdamW PASS，确认严格串行；修复结束通知不再改写训练退出码。8P [Run 38044923848](https://github.com/depeng1994/torchtitan-npu/actions/runs/38044923848)：Muon 5 steps/TB/GitHub PASS；AdamW 进程被 SIGKILL（rc=137），设备释放检查不确定而报告 INFRA_ERROR 并记录隔离，尚未查明 kill 原因。16P [Run 38046480324](https://github.com/depeng1994/torchtitan-npu/actions/runs/38046480324)：原子选取 A3-3 0–7 + A3-4 8–15、双机启动和 dp_shard=16/ep=16 Mesh 完成，但 HCCL 报 `Communication_Error_Bind_IP_Port (EI0020)`，地址 `192.168.3.186:16666` 绑定失败，未完成5步。失败后 GitHub 无在途任务，两个执行容器未见本次训练 Rank；V2 隔离记录留存，未手工删除。旧 Agent 在旧工作区重新启动（源码 SHA `2a0427c`，生产配置逐文件比对无变化），经核对只有一个旧进程且无测试分支授权环境变量；模型仓测试分支临时 Workflow 许可已撤销回 master-only。Lite [Draft PR #1](https://github.com/depeng1994/lite-actions/pull/1)、模型 [Draft PR #28](https://github.com/depeng1994/torchtitan-npu/pull/28) 保持 Draft、未合并。**P1.2 双 PASS、P1.3 全部分组、P1.4 16P/异常恢复均未达到正式 Gate；下次窗口须先定位 rc137 与 HCCL 16666，再经预检实机回归。**


## 8. Phase 2.5 增量详细设计：同 Node 多任务、双 16P 与大集群优先调度（2026-10-12）

> **状态：设计待实施（Design Only）**。本节是 Phase 2 的新需求及下一轮实现契约，**不是已部署功能或验收结果**。现行生产基线：Lite Actions 执行代码 `78015f9`，模型仓 `e465e79`；后续纯文档 Commit 不改变执行代码。已有跨 Host 2×8P / 单独 16P 实机通过，证据见第 7 节。**A3-3 8–15 尚未 verified；第二条 16P 对角拓扑尚未 verified；A5 64P 仍 disabled**。这些必须先过 Gate，禁止直接改 `verified=true`。

### 8.1 目标、范围和硬约束

**新需求**（一个 A3 Node 16 个逻辑设备，按 `0–7`、`8–15` 两个 8P 组切分；所有设备 ID 必须以现行 `card-die` 映射和实机检查为准）：

| ID | 目标场景 | 建议拓扑（示意，不等于全部已验证） | 成立前提 |
| --- | --- | --- | --- |
| N1 | **A3-3 上 8P + A3-3/A3-4 上 16P 同时运行** | 8P = A3-3 `0–7`；16P = A3-3 `8–15` + A3-4 `0–7` **或** `8–15` | A3-3 `8–15` 的 8P 验证，以及对应新的双机 16P 组合单独验收 |
| N2 | **两组 A3-3+A3-4 16P 同时运行** | 16P-A = A3-3 `0–7` + A3-4 `8–15`（已验证单任务）；16P-B = A3-3 `8–15` + A3-4 `0–7` | A3-3 第二组、新 16P-B 的 HCCL/EP16 单任务验证；两机均通过 Host/端口/IO 容量验证 |
| N3 | **单 Node 2×8P 同时运行** | A3-4 `0–7` + `8–15`；后续 A3-3 `0–7` + `8–15` | 两组不重叠、同机端口完整隔离、Host 预算满足；A3-3 第二组验证后才可放开 |
| Q1 | **全局优先：64P > 16P > 8P** | 按受信任的 `nnodes × devices_per_node` 计算资源需求，从高到低进行调度准入 | 64P 即使排队权重最高，也必须先通过 A5 独立的硬件/安全上线 Gate；**禁用任务不得进入抢占或资源预留** |

**不做的事**：不启用 A5，不接受未经签名的拓扑/端口/priority 输入，不抢占正在训练的 Job，不为了凑 16P 而把同 Node 两个 8P 误算作两个 Node，不直接放宽当前 `experimental_multinode_allocations`，不默认允许四训练并发，不通过用户进程名猜测回收对象。

**关键不变量**：

1. 每个 Case 的 **DeviceSet** 是物理 `(host_id, chip_id)` 集合；并发 Case 的 DeviceSet 必须两两不交叠。16P / 64P **一次获取全部 Host 资源**，获取失败时释放所有临时锁，不得持有半套资源等待。
2. **PortSet = (canonical physical host + network namespace, protocol, concrete port/range)**。同一宿主的两组训练即使 NPU 不重叠，端口集合也必须互斥；单个分布式 Case 使用同一租约身份并覆盖其全部参与节点。不能只靠不同 `MASTER_PORT` 推断 HCCL 安全。
3. Host 预算至少考虑 `max_jobs_per_host`、RAM/CPU/IO/网络和训练类别，缺少可靠资源测量时仍按每 Host 1 Job；不得把 “16 个 NPU 空闲” 当成同时运行两份 8P 的充分条件。
4. Quarantine / 未清理的远端 Rank 优先于任何优先级；未知残留只能阻止对应 Host/Device/端口重用，若范围未知则 fail-closed，不允许重启 Dispatcher 后因文件锁释放而跳过。
5. 每个 `repo + run_id + attempt + sha + pipeline + test_id` 唯一运行、唯一上报；取消/超时仅清理本 Case 和本 Case 的端口、进程、租约，绝不误杀同 Node 其他 Case。

### 8.2 现有实现核对与缺口（以 Lite `78015f9` 为基线）

| 代码位置 | 已有能力 | 增量改造点 |
| --- | --- | --- |
| `unified_agent.py` | `MAX_WORKERS=4`、`TRAINING_WORKERS=3`、每个 Matrix Case 独立 Future；当前按 `run_id` 升序提交 | **不能只把循环改为反向排序**：线程池先入先出、等待任务占满 Worker、跨轮询新增 16P/64P 都会破坏优先级。应引入持久化 Pending Queue + 单一 Admission Planner，准入成功后才交训练 Worker |
| `ci_core/scheduler.py:candidates()` | 单机 verified Group；多机仅取 `verified_multinode_allocations`，当前只含一组 16P；可优先空闲 Host | 为 N1/N2 新增候选 Bundle，**先通过测试再进入 verified 清单**；资源组选择须与大任务优先预留/回填联动 |
| `ci_core/resources.py:reserve()` | 跨工作目录 `flock` 设备原子锁 | 增加 Host 预算与网络命名空间级复合租约；资源获取过程使用确定性锁顺序，确保无死锁、无未释放的部分租约 |
| `ci_core/resources.py:reserve_ports()` | 256 个逻辑 slot；`master_port=16000+slot`，`hccl_if_base_port=20000+64*slot`，对参与 Host 使用同 slot 锁 | **现有锁只保证参与 Lite 的逻辑 slot 不重复，不能保证实际 Host/NPU 端口范围不冲突**；需要完整端口画像、Host/NPU 端口段与远端实际监听预检，不能机械扩 slot |
| `ci_core/ssh_runner.py`、模型 `nightly_all_models_test/runner.py` | Dispatcher 传递 `LITE_CI_MASTER_PORT` / `LITE_CI_HCCL_IF_BASE_PORT`，模型覆盖默认值 | 向所有 Rank、所有 Node 按同一 Case 租约传递完整通信端口配置，并验证 `set_env.sh`、训练脚本等没有再次覆盖；8P 的本机 `run_tests/GPUPool` 启动路径也必须覆盖 |
| `ci_core/job_state.py` / Dispatcher | 运行、取消、结果状态与部分 `allocation` 信息持久化 | 新增 `queued_at` / 优先级 / 最终 DeviceSet / PortSet / 租约 owner / 启停时间 / Release 证据；重启后用远端事实对账，不凭本地 flock 解锁宣称释放 |

### 8.3 设备拓扑准入：先验证 A3-3 8–15 和第二套 16P

**现有事实**：`config/pools.json` 中 A3-3 `0–7 verified=true`、`8–15 verified=false`；A3-4 两组均 `verified=true`。现有正式 16P 只有 **A3-3 0–7 + A3-4 8–15**。A3-3 8–15 + A3-4 0–7 目前列在 `experimental_multinode_allocations`，不是正式可用资源。

实施顺序：

1. 只读核对 A3-3 8–15 的 `npu-smi` chip/card-die 映射、设备健康、PCIe/HCCS、Host/RDMA 网卡、NPU/Host 现有占用以及必要的宿主资源。不得杀未知 PID。
2. **独立 8P 5 steps** 验证 A3-3 8–15：Muon 与 AdamW 分别 `exit_code=0`、TensorBoard 步数通过、设备释放、无 quarantine；满足后只批准该单机 Group。
3. **独立 16P 5 steps** 验证新 Bundle（A3-3 8–15 + A3-4 0–7，及 N1 需要的其他非既有配对）：确认 Rank、`NODE_IPS` 顺序、Master 选择、EP16、HCCL 小网、TB 和释放；**单机两组分别 PASS 不等于其跨机组合 PASS**。
4. 仅在对应拓扑通过后，更新 `verified_multinode_allocations`；`experimental` 不自动转正式，`LITE_ACTIONS_ALLOW_EXPERIMENTAL_TOPOLOGY` 生产仍为 0。按场景启用 `max_jobs_per_host=2` 前需独立完成第 8.4 节端口和 Host 预算门禁。

### 8.4 Host + HCCL + NPU 端口完整隔离方案（本次核心）

#### 8.4.1 先确定真实端口集合，不能只改两个变量

现行端口逻辑覆盖 `MASTER_PORT` 和 `HCCL_IF_BASE_PORT`，但历次存在过 `Communication_Error_Bind_IP_Port` 及 **NPU 侧 16666** 端口冲突。昇腾官方资料明确：

- `HCCL_IF_BASE_PORT` 在 Host 侧使用**连续端口段**，具体占用数量受 CANN 版本影响：部分文档为 16 个端口，新版本为 32 个端口。必须以当前**实装 CANN/torch_npu** 为准确认，而不是仅按 base port 1 个端口上锁。
- `HCCL_HOST_SOCKET_PORT_RANGE` 可定义 Host 端口范围，优先级高于 `HCCL_IF_BASE_PORT`（适用版本/算子须检查）。
- `HCCL_NPU_SOCKET_PORT_RANGE` 对 NPU 侧根节点通信可指定范围或 `auto`；未设置时可能使用默认 **16666**。不能假定仅修改 Host 端口会自动隔离该端口；A3/MC² 场景的具体支持约束必须验证。

**参考官方文档**：
[HCCL_IF_BASE_PORT / 连续端口段](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850alpha002/maintenref/envvar/envref_07_0074.html)；
[HCCL_HOST_SOCKET_PORT_RANGE / 优先级](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850/commlib/hcclug/hcclug_000093.html)；
[HCCL_NPU_SOCKET_PORT_RANGE / 默认 16666 / auto](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/900/maintenref/envvar/envref_07_0144.html)。

#### 8.4.2 端口租约数据模型

新建 **每个物理 Host 的 PortProfile**，由部署端可信配置定义，禁止由 PR 自带 JSON 指定任意端口；至少包含：

```json
{
  "host_id": "a3-3",
  "network_namespace": "host",
  "max_jobs_per_host": 1,
  "port_profiles": {
    "hccl_host_range_width": "determined_by_installed_cann",
    "hccl_npu_port_strategy": "explicit_range_after_validation",
    "slots": ["operator_verified_slot_0", "operator_verified_slot_1"]
  }
}
```

这是**目标 Schema 示意**，不是可直接投入生产的 `pools.json`。每个 slot 必须映射互不重叠的（1）torchrun `MASTER_PORT`；（2）Host HCCL 全部监听/预留段；（3）NPU HCCL 监听段；（4）如该模型实际使用的额外 HCCL/RDMA/控制端口段。Host/NPU 网络命名空间与路由来源要明确；现有 `--network host` 使两个容器共享宿主端口空间，容器隔离不等于端口隔离。

分配算法采用 **Host 端口区间租约**（而非仅 slot 整数比较）：若两个 Case 在同一 Host/相同 netns 的 TCP/UDP/Host HCCL/NPU 端口区间存在交集，视为不可并行。同一 Case 的两个 Host 可以使用相同端口数值，但须分别持有两个 Host 的租约。取得所有参与 Host 的端口许可后，连同设备锁和 Host 预算一起原子提交；失败必须整体释放。

**端口预算示意（非生产定值）**：

| Case | `MASTER_PORT` | Host HCCL 区间（按保守 64 宽度预留） | NPU HCCL 区间（须先验实际支持） |
| --- | --- | --- | --- |
| 同 Node Job A | 18000 | 30000–30063 | 50000–50063 |
| 同 Node Job B | 18001 | 30128–30191 | 50128–50191 |

以上数字只说明**不同 Case 的完整区间互不重叠**，不是已经验证能运行的生产端口；实际范围应经网络策略、`ip_local_reserved_ports`、`ss`/`/proc`、NPU 侧占用、CANN 版本及两路 HCCL 压测确认。尤其 `HCCL_NPU_SOCKET_PORT_RANGE` 的 `auto` 不能在未测时直接认作隔离成功，存在 MC²/算子不支持时必须采用可验证替代方案，无法证明则拒绝同 Node 并发。

#### 8.4.3 执行、预检与释放闭环

1. **候选选取**：先选完整 DeviceSet，再选全部 Host 的 PortSet + Host 预算；跨节点端口段在所有 Node 上可用才整体准入。
2. **启动前预检**：核查该 Case 的 Host/NPU 端口确实未被外部非 Lite 进程占用；所有执行机读取同一 `port_lease_id` / Profile，避免 Node0 与 Node1 各自随机端口；并发第三方作业存在不可消除的 TOCTOU 风险，需对绑定失败 fail-closed、完整清理后重新调度而非直接重试训练。
3. **环境注入**：模型仓启动器必须落实到真正启动的 torchrun 和每个 Rank（单机 8P 和多机 16P 均覆盖）。Dispatcher 发 `LITE_CI_MASTER_PORT`、`LITE_CI_HCCL_IF_BASE_PORT` 以及新定义的可信 `LITE_CI_HCCL_HOST_PORT_RANGE` / `LITE_CI_HCCL_NPU_PORT_RANGE`；模型按验证通过的版本转换成对应 HCCL 环境变量。拒绝 `OverrideDefinitions.env_vars`、`set_env.sh`、`scripts/run_train*.sh` 后续无声覆盖；日志只记录非敏感端口 Profile 和实际生效值。
4. **资源全周期持有**：从 staging / preflight / 启动 / 训练 / 停机到 remote child cleanup、端口与 NPU 复查结束，均持有完整设备、Host 预算、端口租约。不可在完成上报前提前释放，尤其不能把另一个并发 Case 的监听视作污染并杀掉。
5. **异常**：绑定失败、HCCL timeout、SSH 中断、Docker 重启或进程无法归属时，记录 `HOST_PORT_CONFLICT` 或具体 `INFRA_ERROR` 与 Slot/Host，不把“分配成功”当作“训练启动成功”。必要时仅 quarantine 本 Case 的真实端口/设备，未知作用域才升级为 Host 级隔离。

### 8.5 调度优先级：64P > 16P > 8P，非抢占式 + 防饥饿

当前 `unified_agent.py` 按 `run_id` 升序发现任务、直接提交到 `ThreadPoolExecutor(max_workers=3)`，属于先提交先排队，**不是**真正可控的资源规模优先级。增量设计：

- **可信入队**：仍然先验 Actor、PR/branch/SHA、plan_digest、Case Test ID；从验证后的 `case["resources"]["nodes"] × devices_per_node` 得出 `requested_devices`，并保存到 JobState：`queued_at`（首次出现时）、`enqueue_seq`、`requested_devices`、`priority_class`、`run_id/attempt/test_id`。不得接受 PR 设置 `priority`、指定 Host/卡号/PortSet 或篡改排队时间。
- **单个 Admission Planner**：负责跨所有 Workflow/Run 的资源仲裁，持有全局**短时调度决策互斥**，按 `(requested_devices DESC, queued_at ASC, stable_job_key ASC)` 决定候选次序；**训练执行本身不持有全局串行锁**。Planner 在准入前取得整套资源，资源就绪才提交给 3 个 Training Worker。64P 已禁用时从合法队列直接排除，不能占住优先级槽或拖慢 A3。
- **非抢占**：已启动的 8P 不因新的 16P/64P 到来被强制停止。若 16P 当前可获取完整资源且 8P 同样待启动，则 16P 必须先 Admission；如果 16P 暂不满足而 8P 可用，允许**安全 Backfill**，但不得无限延长已等待 16P 的完成窗口。
- **保留与公平性**：为最老的高优先级可配置 `reservation_grace_seconds`（初始建议 60–120s，必须经压测选定）；预留的是**调度意图与即将空出的资源组**，不是先拿住某一 Host 的真实 flock 再等第二 Host。Backfill 必须证明不延后高优先级的可行完整 Bundle；无法证明预计完成时间就停止相交资源的 Backfill，避免新 8P 无限挤占 16P。对同等级 Case 用 FIFO；更低级 Case 的等待时长在指标中显式监测，必要时引入公平配额/老化但不可悄悄反转显式的 64>16>8 顺序。
- **Worker 与取消**：等待资源不应长期占住 Training Worker；引入 `QUEUED/WAITING_RESOURCES/ADMITTED/RUNNING/PENDING_REPORT/REPORTED` 可观测状态，Worker 只运行已 Admission 的 Case。每轮重新检查 GitHub Run 是否取消、Attempt 是否变化、SHA 是否过期；资源准入与状态转移使用幂等 owner CAS 或单一计划锁，避免并发重复派发。

**示例**：16P 和两条 8P 同时入队，若 N2 所需完整双机资源已空闲，则先给 16P；剩余 8P 再根据不重叠设备分配。如果 16P 的第二组未验证，属于**不合法候选**，不能无限阻止两个可运行 8P。一个 64P 即使计划权重最高，也要先满足 A5 的 enable/硬件 Gate，否则既不运行也不预留。

### 8.6 分批实现与变更文件

| 阶段 | 变更范围（计划） | 退出门槛 |
| --- | --- | --- |
| D0｜清查拓扑/端口 | 调度机、两 A3 设备组、当前 CANN/HCCL/torch_npu、端口监控；`config/pools.json` 仅在验收后改 verified | 取得第二组 8P 与新 Bundle 的受信任证据、Host RAM/CPU/网络和端口用量基线；不占用生产 Job |
| D1｜复合租约 | `ci_core/resources.py` / `scheduler.py` / `config/pools.json` / `ci_core/config.py` | 设备+Host 预算+全端口范围的原子租约，多进程冲突、端口碰撞、失联/取消测试通过 |
| D2｜Case 优先准入 | `unified_agent.py` / `ci_core/dispatcher.py` / `ci_core/job_state.py` / `ci_core/reporting.py`（视需要） | 64>16>8、FIFO、跨 Workflow、非抢占、安全 Backfill、无重复领取；50–100 个模拟 Case 压测 |
| D3｜模型适配 | Lite `ci_core/ssh_runner.py` / `dispatch.py`；模型 `tests/integration_tests/nightly_all_models_test/runner.py`、8P/16P 启动链、单测 | 单机和双机端口 ENV 全链路生效，Host/NPU 监听经实机核实，模型脚本不覆盖 |
| D4｜分级硬件验收 | Lite 与模型仓测试及 `REFACTOR_PLAN.md` 台账 | 按 N3 → N1 → N2 启用，各 Case 独立 5 steps/TB/rc0、时间重叠、端口冲突 0、NPU 释放和无 quarantine |
| D5｜灰度/回滚与文档 | 两仓 README、调度端部署 runbook、配置文档、版本与 Run 链接 | 旧 Dispatcher 停接单，待在途 Case 安全结束后切换；不改历史状态、不热替换训练；可回滚到 `78015f9` 资源并发基线 |

**发布默认值**：新机制合入代码后仍 **`max_jobs_per_host=1`**、新 Bundle 不启用；N3 单 Host 验证通过后仅对应 Host 灰度为 2。N1/N2 各自独立 Gate，未通过则相应场景始终不调度。优先级机制可先在已验证的跨 Host 场景上线，但 A5 继续禁用。

### 8.7 必须完成的测试矩阵（新增 Gate）

| ID | 类别 | 输入 / 故障 | 验收标准 |
| --- | --- | --- | --- |
| N01 | 拓扑 | A3-3 8–15 单独 8P | `npu-smi` 映射、Muon/AdamW 5 steps + TB + rc0；无未授权进程清理 |
| N02 | 拓扑 | 新 A3-3 8–15 + A3-4 0–7 双机 16P | 先独立 5 steps、EP16/HCCL/全部 Rank/TB PASS，才能转 verified |
| N03 | 并发 | A3-4 单 Node `0–7` + `8–15` 两条 8P | 同时 RUNNING 且各自独立成功，真实监听端口段不重叠，Host RAM/IO 预算达标 |
| N04 | 并发 | A3-3 8P 与 A3-3+A3-4 16P | 两个 Case 在 A3-3 设备组、PortSet 不重叠，16P 双机 Rank 正常，双方 5 steps/rc0/TB |
| N05 | 并发 | 两条 A3-3+A3-4 16P | 同时训练，各占两机独立 8P，Host HCCL + NPU port range 隔离，双 Case PASS |
| N06 | 端口 | 两条 Case 配到同 `MASTER_PORT` / 交叠 Host 或 NPU 区间 | Admission 阶段拒绝相交 PortSet，不启动训练、不泄漏锁 |
| N07 | 端口 | 跨节点仅一个 Host 端口不可用 / 外部进程占用 16666 | 原子失败并换合法 Slot 或等待；不会在另一 Host 留锁、不 kill 未授权 PID |
| N08 | 调度 | 64P/16P/8P 同时 Pending（64P 仅模拟） | 同时合法可调度时准入顺序 64>16>8；64P disabled 时不得阻塞 |
| N09 | 调度 | 16P 长等待，持续到达 8P | 无永久饥饿；非抢占、保留窗口内 Backfill 不延误高优先级 Case；同优先级 FIFO |
| N10 | 容错 | 2×8P / 2×16P 其中一条 Cancel、SSH 断、HCCL Bind Fail | 仅故障 Case 退出/清理/必要隔离，另一 Case 继续 PASS，无误杀/假 PASS |
| N11 | 恢复 | Dispatcher 意外退出、远端 Rank 尚在、主机/容器重启 | 重启后依据进程/端口/台账对账，未知资源 fail-closed，不因 flock 自动释放误复用 |
| N12 | 回滚 | 新 Host 配额、端口 Slot、Planner 发布失败 | 在停止接单和安全清理后退回上版；重新 Smoke + 8P/16P 回归，历史结果/隔离仍可追溯 |
| N13 | 压力 | 50–100 可信模拟 Case：跨 Run/Attempt/8P/16P/64P | 设备/PortSet/Host 预算不重叠，无双重上报、无死锁/饥饿、API 限流可恢复 |

**全链路证据格式**：每个 Test Case 记录 Run/Attempt/SHA、`test_id`、真实 Host/DeviceSet（对外脱敏）、PortProfile/Slot（无凭据）、真正训练起止时间、Steps/TB、各节点 Exit Code、Job/Comment 最终状态、清理检查与内存/网络峰值。CI 绿灯、孤立的单 16P PASS、或仅显示两个 Job `in_progress` 均不足以判定 N04/N05 并发通过。

### 8.8 风险与发布阻断

- **端口范围与 CANN 版本错配**：Host HCCL 16/32 端口版本差异、NPU 16666 默认、额外 RDMA/HCCL 端口、环境变量的支持矩阵；未形成完整 Profile 前一律 `max_jobs_per_host=1`。
- **设备组未验证**：A3-3 8–15 当前未验收，不许直接置 `verified=true`；新 16P Bundle 不得从 experimental 静默放行。
- **Host 过载与性能回退**：内存、CPU、网络与磁盘 IO 会互相干扰；真实 NPU DeviceSet 不重叠只是必要条件。超过 Host 预算退回单 Host 1 Job。
- **高优先级饥饿及队头阻塞**：队列公平性按端到端 Admission 日志证明；不可将无法执行或 disabled 的 64P 作为全局锁。
- **跨 Case 回收误杀**：按 `LITE_CI_JOB_ID`、PID 启动时间、命名空间归属定点清理；设备和端口仍被其他 Case 租约持有时禁止清理。
- **时序/恢复竞态**：抢卡、端口绑定检查 TOCTOU、取消与报结果并发、Dispatcher 重启后锁释放；必须可幂等恢复且无重复训练。
- **双主源同步风险**：更新模型仓启动器后对照 GitCode/GitHub 真实 SHA；不擅自开启 Sync Upstream，不提前合并组织仓 PR，也不改变 A5 禁用状态。

### 8.9 新需求台账

| 里程碑 | 当前状态（2026-10-12） | 代码 / 验收证据 | 下一步 |
| --- | --- | --- | --- |
| P2.5-DESIGN 三种同 Node 并发 + 优先级 | **设计完成，待评审和实施** | 本节；执行代码仍是 Lite `78015f9` / 模型 `e465e79` | 维护者确认设计与安全边界 |
| P2.5-TOPO A3-3 第二组与新 16P Bundle | **未验收** | A3-3 8–15 `verified=false`，新对角 16P 在 experimental | N01、N02 |
| P2.5-PORT 全部 Host/NPU HCCL 端口隔离 | **未实施** | 现行仅两项端口变量与逻辑 Slot；缺少 NPU 16666 及 Range 证明 | N06、N07、N03 |
| P2.5-QUEUE 64>16>8 统一调度准入 | **未实施** | 当前按 Run ID 入线程池 | N08、N09、N13 |
| P2.5-NODE 同 Node 两条 8P | **未正式验收** | 已有同 Node 独立训练记录，不等于完整 Host 预算/端口 Gate | N03、N10、N11 |
| P2.5-MIX 8P + 16P 同节点混部 | **未验收** | — | N04 |
| P2.5-2x16 两条双机 16P | **未验收** | — | N05 |
| P2.5-RELEASE 灰度、故障与回滚 | **未验收** | 保留稳定的跨 Host 2×8P / 独立 16P 基线 | N10–N13，按通过场景逐项启用 |

**本轮提交范围**：仅在既有 `REFACTOR_PLAN.md` 增加详细设计和状态记录，不改执行代码、资源池、Workflow、生产 Dispatcher，不启动或取消任何 Actions。
