# TorchTitan GitCode Forward：原设计逐项审计与实施检查点

> **最新续办记录见第 10 节**：Worker `d9444a2`、模型 `67cee7f` 已提交并完成精确提交 CPU 回归；完整 TorchTitan GitCode Forward 仍未验收。第 1～9 节保留上一检查点的逐项审计，不将新单测替代原 E2E/INT。

**检查日期：2026-11-02。总体结论：尚未完成，不能按原设计 DoD 宣布交付或开启 TorchTitan schema-4 生产准入。**

本审计以 `gitcode-forward-design.md` V3.1 的 G01–G14、R01–R11、P0–P6、E2E-01–18、INT-01–09 为基准，并应用 `gitcode-forward-scope-20261102.md`：vLLM 只验收 GitHub 原生链路，不再要求 GitCode Bridge、GitCode 状态回写或 U→C 同步。被取消的范围标记为 OUT_OF_SCOPE，不改记为 PASS。

## 1. 不可变版本与本轮证据

| 对象 | 版本 / 结果 |
|---|---|
| Lite Actions 实现分支 | `depeng1994/lite-actions:gitcode-forward` |
| 本轮 Worker 代码提交 | `777a2b6ef44c9283f8122379c8c4f9ea666fe6fb` |
| Worker tree | `ba407603c8ed147028923346772ec2fae8a0e563` |
| TorchTitan 个人开发分支 | `depeng1994/torchtitan-npu:gitcode-forward` |
| 本轮模型 Runner 提交 | `efe4e701226000223c341507ffea51199cbe0e9d` |
| 模型 Runner tree | `811d9e61b0d1361fd516609b3f030c2e009d9be0` |
| 独立 worktree 的 Worker 全量回归 | **300 tests，OK**；CPU、本地 Git、Mock 与本地子进程，不是平台/NPU E2E |
| Worker 静态检查 | Ruff/lint PASS，74 个已跟踪 Python 文件格式通过，compileall PASS |
| 独立 worktree 的模型 Runner 测试 | **14 tests，OK**；相关 Ruff/compile PASS |
| 两仓合同检查 | 实际模型 Runner Bundle 与 Worker 校验器一致；Smoke 8P/16P、Muon/AdamW 8P、AdamW 16P 五个接口通过静态验证 |
| 本轮新硬件任务 | **0**；容器检查为只读 |
| 当前生产 Worker | 仍为 `f4b15e32b8348d0762cb808a85f7ea0a6f33745a`，服务 active；本轮未切换 |

精确提交验证时间为 `2026-11-02T03:02:16.782078+00:00`。机器可读版本、日志摘要和边界见 [验收记录](evidence/torchtitan-gitcode-777a2b6.json)。

前置 TorchTitan PR #11、Worker PR #14 已重新查询，均已 merged；不再是等待项。它们此前的训练/Smoke 结果不代替本项目的 GitCode 来源、事件、合并树和回写证据。

## 2. 本轮实际新增的实现

### 同一 Router 的 schema-4 接入

`ci_core/gitcode_runtime.py` 将已有 Admission Broker、SQLite 协调器、保留式 Source/Runner 存储和固定 Check Run ID 通道接入 `Router` 的实际接收、排队、工作线程、资源租约、取消和结果阶段。仍使用同一设备/端口锁与 JobState，不另起调度器，不建立第二套物理锁域。

Prepare 不成功不入队；已撤销代际或已释放 fencing token 不得启动；schema-4 不调用 schema-3 的模型目录发现、GitHub 源码回退或 Commit Comment 回报。所有必需 Case 完成后才可汇总。平台运行已取消时，不新增 aggregate PASS。

### 独立可信 Runner 的远端执行后端

`ci_core/gitcode_execution.py` 消费保留式、摘要已验证的 SourceHandle 和 Runner Bundle。控制机只做安全解压、AST/静态接口和目录校验，不运行源码包中的 Python。各节点按捕获的容器 ID 校验、分别传输 Source/Runner、重新校验摘要；全部 staging 完成后再次检查版本/租约才启动。

远端执行清空继承环境，使用受信 Python 与固定 Runner、完整端口租约、绝对 deadline。逐节点回读来源 SHA/tree、lease/fence、rank/world size、退出与测试证据。Blue 只检查外部 NPU 占用，不强制回收外部进程；清理无法证明时隔离。

`ci_core/gitcode_supervisor.py` 增加远端持久化取消标记和启动锁。取消与 Popen/PID 记录串行化，网络中迟到的启动请求不能在取消后重新启动。测试实际执行本地受控子进程验证这些语义，但尚无 NPU/远端断连故障实证。

### 恢复与回报

协调器账本升级为 **state_version=3**，事务迁移保留原代际、请求、租约及 outbox。新增固定 ID 汇总回报确认：完成历史不再每轮查询 Check 槽，丢响应只重试回报，不重跑训练。接单后的重复发现不重新下载 Artifact。

恢复可从 SQLite 重建丢失的 JobState 投影；已有 CLAIMED 任务不重新执行；捕获租约未证明释放时继续隔离；Artifact 删除不妨碍已有结果恢复。未知协议拒绝启动。原 schema-3 隔离对象在集成测试中保持不变。

**启动装配尚未完成：** `Router` 的可选 adapter 与默认远端 backend 已实现，但 `unified_router.py` / systemd 尚未装配真实 GitCode 权威事件适配器、App 令牌供应器和批准发布策略。不能仅设置一个 enabled 标志就绕过这些前置，也不能把依赖注入测试称为生产已接入。

## 3. G01–G14 目标核对

| ID | 当前结论 | 尚缺的证据 / 工作 |
|---|---|---|
| G01 | 未完成 | 真实 GitCode PR/Action 自动触发，固定事件与 GitHub Run 链接 |
| G02 | 部分实现 | H/B→M/T 与安全消费有 CPU/本地 Git 测试；缺真实 GitCode PR 到 NPU 的一致来源证据 |
| G03 | 未完成 | 官方 GitCode 控制发布 U、默认分支同步 C=U、完整文件与注册核验 |
| G04 | CPU 集成通过，平台未验收 | 实际 Router 测试证明 Prepare 失败零入队/零设备；缺 Hosted 准入实证 |
| G05 | 未完成 | TorchTitan GitCode→GitHub→Blue→GitCode 当前版本闭环 |
| G06 | CPU/故障注入覆盖，平台未验收 | 冲突、坏包、旧代际、缺 Case、清理不明不误绿；真实平台负向链路待跑 |
| G07 | 回归通过，升级未验收 | schema-3 相关全量回归通过；新候选上的真实旧入口及迁移/回滚待验收 |
| G08 | 本轮保持范围边界 | 未修改/调度 Yellow；Native Self-hosted Adapter 未实现、未注册，符合本期排除范围 |
| G09 | 按范围修订处理 | vLLM GitHub 原生正向/失败恢复已有生产证据；GitCode 子要求 OUT_OF_SCOPE |
| G10 | vLLM 已有生产 PASS | OS/8P/16P 与固定容器证据见 vLLM 专项生产记录，不外推为完整推理 |
| G11 | 部分验证 | 共用真实资源锁的 CPU 集成、既有原生双仓运行快照；缺 TorchTitan GitCode 与 vLLM 的实机交叉验收 |
| G12 | 合同已实现，GitCode 平台未验收 | versioned_only 不冒充原子合入；真实 Base-only 失效、回写及延迟仍缺 |
| G13 | 候选代码/本地故障测试通过 | 排队撤销、启动前代际/租约校验、远端启动取消锁；缺真实平台运行中取消与事件顺序实证 |
| G14 | 本地恢复验证通过，生产未验收 | SQLite/outbox/投影恢复与隔离保留；真实 drain、迁移、崩溃恢复、回滚待跑 |

## 4. R01–R11 文件责任核对

| ID | 已有实现 | 尚未完成 |
|---|---|---|
| R01 控制发布/镜像 | 发布身份验证库；前置 PR 已 merged | GitCode 官方 U 的用户审查合入及 C=U 同步记录；未改写镜像策略 |
| R02 个人仓 Trusted Bridge | nonce 派发账本、权威 PR 客户端等库与测试 | GitCode 个人 Base 上的可信 Workflow/脚本安装、真实事件/运行身份校验、状态发布 |
| R03 合并源码 Provider | 固定 H/B、本地合并、冲突拒绝、安全归档、依赖/接口检查 | GitCode 平台凭证与真实 PR 取源实证、NPU 来源一致性 |
| R04 派发/认领/重跑 | 不可复用意图、跨投递 canonical 认领、代际与 outbox | GitCode 持久化事件来源、授权整单重跑和丢响应平台实证 |
| R05 Prepare/产物门槛 | Producer、ArtifactReader、Manifest/Request、固定槽和完整 Prepare 检查 | Hosted 制品上传适配、Workflow/Prepare/Waiter 启动编排及正式发布 |
| R06 Blue 安全准入/SourceHandle | 实际 Router adapter + 独立远端执行 backend，禁止 PR 目录发现 | 真实权威/App/策略配置加载与服务入口装配；新路径实机验收 |
| R07 版本结果通道 | 固定 Check ID、receipt、完整 Case 聚合、持久回报确认 | 同一专用 App 的平台创建/更新实证，GitCode 当前 H/B 状态回写 |
| R08 迁移/回滚 | state v3 事务迁移、捕获租约、恢复隔离、未知版本拒绝 | 生产切换前兼容启动门槛与回滚规程实证，不能直接回退到不识别新状态的旧二进制 |
| R09 Latest-wins/Base 补偿 | 代际撤销、排队取消、运行检查、迟到启动 interlock | GitCode Base push/周期任务、真实事件顺序、运行中停止/失效延迟 |
| R10 Self-hosted 预留 | SourceHandle、Runner、Manifest 和资源/结果接口保留 | 本期不实现 Adapter，不新增硬件验收目标 |
| R11 故障隔离/限额 | per-run 故障隔离、固定槽、保留 outbox、完成历史不反复查询 | 平台限流/短期 Token 轮换及一仓平台故障下另一仓实机完成的证据 |

## 5. P0–P6 阶段门槛

| 阶段 | 状态 | 退出标准是否满足 |
|---|---|---|
| P0 事实/能力 | PARTIAL / 接入受阻 | 否：GitCode 认证/事件、同一专用 App、U→C 发布均未实证 |
| P1 协议/控制代码 | PARTIAL | 否：库和可信 Runner 已测试；Hosted 编排、GitCode Bridge/状态适配未完整发布 |
| P2 Blue 适配 | IMPLEMENTED_CANDIDATE / NOT_DEPLOYED | 否：实际 Router/远端 backend 有 CPU 集成；启动装配、NPU/生产迁移尚未验收 |
| P3 上游发布/联调就绪 | NOT_READY | 否：尚无可信 U/C、平台适配器与 App 就绪证明 |
| P4 分层 E2E | TORCHTITAN_GITCODE_NOT_RUN | 否：vLLM 专项成功不替代 TorchTitan GitCode 链路 |
| P5 正式发布 | TORCHTITAN_SCHEMA4_NOT_DEPLOYED | 否：现有生产版本继续提供原生 schema-3 路径 |
| P6 交付关闭 | INCOMPLETE | 否：不能关闭原设计 DoD |

## 6. 18 项 E2E：逐项记录

| ID | 场景 | 当前验收状态 |
|---|---|---|
| E2E-01 | TT GitCode PR → OS+8P | NOT RUN：无本期真实 GitCode 闭环 |
| E2E-02 | TT Head A→B 主动取消 | NOT RUN：现有 CPU 代际/Router 测试不是实机取消 |
| E2E-03 | TT Smoke 真失败 | NOT RUN：不能用 Prepare 拒绝替代 |
| E2E-04 | TT 安全拒绝零任务 | CPU COVERED，平台 NOT ACCEPTED |
| E2E-05 | TT Muon/AdamW 五步训练 | NOT RUN：前置 PR #11 的历史训练不计入 |
| E2E-06 | TT GitCode 双节点 16P | NOT RUN |
| E2E-07 | vLLM 原生 PR OS+A3 8P | PASS_IN_REVISED_GITHUB_SCOPE：Run 41369868409/1 |
| E2E-08 | vLLM 原生双节点 A3 16P | PASS_IN_REVISED_GITHUB_SCOPE：Run 41369868409/1；SUM=120，清理确认 |
| E2E-09 | vLLM Head 更新主动取消旧任务 | NOT FULLY ACCEPTED：真实运行中更新取消仍缺；失败后恢复不能替代 |
| E2E-10 | vLLM 越权/错仓拒绝 | CPU COVERED，完整平台零执行验收未关闭 |
| E2E-11 | TT GitCode 与 vLLM 同 Blue 互斥/隔离 | PARTIAL：原生双仓和 CPU 共享锁证据已有；目标混合链路未跑 |
| E2E-12 | vLLM 真实 NPU 失败与清理 | PASS_IN_REVISED_GITHUB_SCOPE：41368985668/1 两项真实 FAIL、清理确认，41369868409/1 恢复 PASS |
| E2E-13 | 官方控制发布 U→C | TT NOT RUN；vLLM GitCode 同步 OUT_OF_SCOPE |
| E2E-14 | 合并树内容与 H/B 共同依赖 | NOT FULLY ACCEPTED：缺设计要求的完整真实差异/Head-only-pass-merge-fail 场景 |
| E2E-15 | 真实 GitCode PR 合并冲突 | NOT RUN；本地 Git 冲突负向测试不代替 |
| E2E-16 | Base-only 失效/取消/自动重测 | NOT ACCEPTED；vLLM 41368791253 已安全拒绝旧预合并父提交，但自动恢复竞争窗口未解决 |
| E2E-17 | 所选门禁模式/竞争窗口 | NOT ACCEPTED：版本化合同不等于 GitCode 平台失效和延迟实证 |
| E2E-18 | 排队/运行取消、乱序与旧回调 | CPU/LOCAL-SUBPROCESS COVERED；平台+NPU必验部分未完成 |

vLLM 证据的详细 H/B/M/T/C、容器 ID、退出与清理见 [vLLM 生产验收](vllm-blue-os-8p-16p-production.md)。本轮没有改写其历史记录，没有将局部成功升级为整项项目完成。

## 7. 9 项 INT：逐项记录

| ID | 已完成的检查 | 仍未关闭的部分 |
|---|---|---|
| INT-01 | 规格/代际/重复事件/丢派发响应的库级和跨阶段 CPU 测试 | 真实 Bridge 持久化与授权整单重跑平台合同 |
| INT-02 | Source/Runner 分离、静态接口、同名模块拒绝、credential-free 子进程，本地负向测试 | 最终 Hosted/真实平台控制凭证隔离实证 |
| INT-03 | Producer→ArtifactReader→Router：失败/部分 Prepare、坏包/摘要、陈旧版本零启动 | 最终 Hosted 制品通道与平台故障实证 |
| INT-04 | state v1/v2→v3 迁移、未知版本拒绝、schema-3 隔离保留的本地测试 | 生产存量 queued/cancel/outbox、独占切换、回滚全过程 |
| INT-05 | 已完成请求在 Artifact 删除后恢复回报；捕获租约恢复不重跑 | 真实远端崩溃/断网后清理与恢复操作验收 |
| INT-06 | schema-4 维护故障不阻断其他轮询、原有跨仓故障回归 | 同一生产 Worker 两条完整链路下的平台故障隔离 |
| INT-07 | 固定槽身份、丢响应/slot 错误、回报确认、历史不反复查询测试 | 专用 App 权限/安装令牌/创建更新/留存容量真实能力 |
| INT-08 | Run/Attempt、控制版本、缺 Case、重复结果/旧发布权等负向测试 | 最终 GitCode Bridge 与 Hosted 全部/部分 rerun 平台交互 |
| INT-09 | vLLM 固定 Name/ID/镜像、源码/执行/清理正向与负向测试 | 全生命周期容器重建恢复与目标混合链路实机验证未完整关闭 |

INT 表列出已通过的具体 CPU 子检查，而不是把所有集成项一概标成 PASS。

## 8. 当前接入前置与剩余开发

### 已实际观察的限制

1. 非 depeng 的 Remote 机器上凭证文件存在；本轮读取并在进程内用于 GitCode API 的工具调用被安全检查拦截。未输出凭证，未经其他凭证路径绕过。随后不带任何凭证查询两个 GitCode 项目均返回 HTTP 401。**这不是“凭证无效”的结论。**
2. 当前 `gh` 登录身份查询 App installations 返回 HTTP 403；生产 Blue 未配置本设计的专用 App token provider。TorchTitan 仓 repository-secret 名称清单只读结果仅有 `DEPLOY_KEY`。这不证明组织继承或其他位置绝无 App，但同一 App 的 Prepare 创建/Blue 更新能力尚未建立。
3. 原设计限定官方 GitCode 上游由用户审查合入。本轮仅提交个人 GitHub 开发分支，没有写入官方 GitCode 上游；没有官方 U、C=U、Workflow 注册的本期发布证据。

### 尚未完成的代码不归因于权限

认证接入和平台事实明确后，仍须完成/发布 GitCode 可信事件与 revision 适配器、个人仓 Bridge/状态发布/Base 补偿、Hosted Artifact 上传与 Workflow/Prepare/Waiter 编排、批准策略/App token provider 的服务启动装配，以及完整端到端验收。当前不是“全部代码完工，只差一个 Token”。

后续上线顺序必须保持：P0 真实合同/能力 → 完整个人与上游控制候选 → 用户上游合入并核验 U→C → Blue 独占/状态兼容装配 → OS → Smoke 8P/16P → Nightly → 真失败/更新取消/混合资源/恢复 → 回滚和交接。不能以修改 Required Checks、空白状态、GitHub 独有控制补丁或 Mock 事件证明绕过门槛。

## 9. 本轮生产边界

只读复核时，Blue 服务 active，94 条任务状态、零在途任务、原 :153 设备 0–7 隔离对象保留。两台 TT 容器 Name/ID/Image/Running 只读检查通过；本轮没有启动新的 NPU 任务，没有重启/切换生产 Worker，没有修改 Yellow、分支保护、Required Checks 或业务 PR 合并状态。

**交付判断：已有可核验的新代码与精确提交测试；完整 TorchTitan GitCode Forward 仍未交付，原设计 DoD 不满足。**


## 10. 本次续办：已提交代码与当前真实门槛

本节机器证据记录时间为 2026-11-02 03:58 UTC（北京时间 11:58）。不是全项目完成声明。两个前置 PR 已经合入，不再作为等待理由。

### 10.1 已提交、精确版本复核

| 仓库/分支 | 代码提交 | 实际验证 |
|---|---|---|
| `depeng1994/lite-actions:gitcode-forward` | `d9444a26911864fc79f1bf756ff5545014e749a6`，tree `cd8ec567b0507922be582ad3401e3cba4b6485e2` | 独立 worktree：330 项全量单测通过；lint 通过，79 个 Python 文件格式通过；compile 通过 |
| `depeng1994/torchtitan-npu:gitcode-forward` | `67cee7fe6df4cc4d81619dce380bb51310c8b906`，tree `804c3fcd88e6d13f91301a7332ab916758158012` | 在非 depeng Remote 的实际 PyTorch 环境检出精确提交：63 项 schema-3 控制回归、14 项可信 Runner、18 项 Hosted 边界测试均通过；本轮新增 Python 静态检查、compile、三个恢复 Workflow 的 actionlint 通过 |

Worker 新增专用 Checks App 短期 installation token、固定仓库/最小权限校验与过期刷新；未知 Check 写入交由原持久化 journal 恢复，不自动重复 POST、不回退 PAT。App 私钥签名测试使用临时生成的合成 RSA 密钥，没有读取生产私钥，不能当作实际 App 权限验收。

恢复此前未提交的 Base 补偿、分页发现和 outbox 公平性实现并提交：已登记 PR 不受单页历史上限影响，发现只是核对信号；缺独立 EventProof 时零 dispatch；回报重试顺序在 I/O 前持久化，坏结果不会永久占满前批队列；state v3→v4 原子迁移保留租约、取消和 outbox。全部为 CPU/本地故障注入范围。

模型补齐 PR #11 的 25 个必要控制/目录/测试依赖文件，与 `f20d073aec6bb7543b2e7db706d1e01b5298c8ca` 对应文件逐字节相同；没有执行旧 YAML 删除。新增 Hosted 不可变 Run/Attempt/C/Workflow SHA 边界与完整验证后按 ID 等待；上传 SDK 固定 6.3.1 和 lockfile，ESM 实际导入检查通过，上传子进程不继承 GitHub/GitCode/App 凭证。

**保留事实：模型新提交还没有接入 GitCode Workflow 分支。** 恢复的是原 schema-3 入口；Hosted helper 不是最终发布编排，不把半套候选推到组织默认分支。尚需真实 GitCode 事件/状态适配、最终 CLI/Job 编排与服务启动装配。这些是未完成开发，不只归因于权限。

### 10.2 当前无法跨越的外部发布门槛

非 depeng Remote 的 GitCode 凭证文件存在且非空。读取该凭证并在进程内调用 GitCode API 的操作被工具安全检查拦截，未取得认证 API 返回；没有打印凭证，也没有换路径/换凭证方式绕过。**不能将此解释为 Token 无效、过期或仓库不存在。** 本轮未写入 GitCode 官方或个人仓。

专用 App 的代码支持已新增，但 Prepare 与 Blue 使用同一专用 App 的真实安装范围、短期 token、创建/更新 Check 能力仍未配置并验收。普通 GitHub 插件可修改文件不等于生产 Worker 已有该身份。

官方 GitCode 控制发布继续按原设计由用户审查合入；当前没有新 U 及 C=U 的平台证据。本候选尚未完成编排，不能把它当作完整上游待合入包，更不能用 GitHub 独有控制补丁绕过。

`@actions/artifact` 锁文件的本轮 npm audit 为 0 high / 0 critical / 7 moderate（archiver/glob/minimatch 链）。固定单文件上传未暴露 PR glob，但依赖风险审查尚需收口；不将审计写成全绿。

### 10.3 严格按原 P0–P6 的当前判断

| 阶段 | 当前状态 | 未完成项 |
|---|---|---|
| P0 | PARTIAL / BLOCKED | GitCode 认证及真实事件/回写合同、专用 App 实证 |
| P1 | PARTIAL | 已补 Hosted 边界与前置控制依赖；真实 Bridge/状态适配、最终 Job 编排未完成 |
| P2 | CANDIDATE / NOT_DEPLOYED | 330 项 CPU 回归通过，服务启动装配和真实执行仍未验收 |
| P3 | NOT_READY | 无完整官方控制发布及 U=C 核验 |
| P4 | TT_GITCODE_NOT_RUN | 本轮新增真实平台/NPU E2E 通过数为 0；第 6/7 节逐项状态未被单测升级 |
| P5 | TT_SCHEMA4_NOT_DEPLOYED | 不满足前置时没有强开生产 Provider；真实迁移/回滚未关闭 |
| P6 | INCOMPLETE | 全项目 DoD 未满足 |

原 18 项 E2E、9 项 INT 的逐项审计继续适用。vLLM 已验收的 GitHub 原生 Blue 三项仍仅覆盖其文档范围，不能冒充 TorchTitan GitCode 实机结果；Base-only 自动恢复等未关闭项保持未关闭。

### 10.4 生产复核和证据

生产 Blue 仍是 `f4b15e32b8348d0762cb808a85f7ea0a6f33745a`，active，PID 142932；94 条状态、零在途任务、原隔离记录保留。本次没有重启生产、没有启动硬件任务、没有改 Yellow/Required Checks/分支保护、没有合入业务 PR。

原始日志分别保留在两台授权机器的 `gitcode-forward-work` 目录。精确提交、日志 SHA256、生产状态、凭证调用边界与剩余任务见 [本轮机器证据](evidence/torchtitan-gitcode-forward-resume-20261102.json)。
