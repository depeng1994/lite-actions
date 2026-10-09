# A3/A5 CI 统一调度设计（实施基线）

## 目标
- GitHub 分层保障：同一 A3-8P / A3-16P Action 串行不丢任务（`concurrency.queue: max`，`cancel-in-progress: false`）；不同流水线物理 NPU 按“宿主机 + 设备 ID”互斥，互不重叠可并发。
- **一个 Pull Agent、一个 GitHub Waiter、一个可扩展 N 节点 Dispatcher**；2P/8P/16P/A5 64P 不分别复制一整套。
- A3 8P、16P 在 GitHub Actions 上完成真实端到端回归。A5 64P 只完成配置/Mock，不做 NPU 实测；无硬件验证不得写 PASS。
- 固定来源 SHA、可信维护者、固定测试白名单；内网仅出站 GET 与有限大小 POST；完整训练日志不上 GitHub。

## 分层/目录所有权
| 层 | 所属目录 | 责任 |
| --- | --- | --- |
| 触发层 | `torchtitan-npu/.github/workflows/*.yml` | workflow_dispatch、固定并发组、等待 |
| GitHub Waiter | `torchtitan-npu/.github/scripts/wait_result.py` | 唯一结果协议：run_id + attempt + sha + pipeline，≤20 行 |
| 控制层 | `lite-actions/` | 单 Agent、GitHub API、状态/去重、限量回传、上传计量 |
| 执行层 | `lite-actions/dispatcher.py` + `resources.py` | N 节点 SSH stage/launch/cleanup、物理资源锁、超时 |
| 测试层 | `torchtitan-npu/tests/integration_tests/` | OverrideDefinitions / CLI / TensorBoard step、有限 Loss、Golden |
| 训练入口 | `torchtitan-npu/examples/` | 模型参数、optimizer、EP、TP/DP、硬件专用量化默认值 |

**禁止**让 lite-actions 维护第二份 EP、GBS、Muon、AdamW 模型参数；也不能让 `OverrideDefinitions.env_vars` 控制 SSH IP/设备锁。当前 `run_tests.py` 将 env_vars 合并后重写 NGPU/LOG_RANK；16P 的两机任务不能硬塞进单机 GPUPool。

## 两级串行/互斥
1. 每个 Workflow 配置 `concurrency: {group: a3-Xp-ci, cancel-in-progress: false, queue: max}`：同用例当前最多一个 Running，最多 100 Pending；队列满按 GitHub 规则取消新增。不同 workflow 各自队列，不能用 GitHub group 代替设备锁。
2. Dispatcher 对 `(hostname, physical_chip_id)` 按字典序取非阻塞文件锁，取得全部资源才继续；部分失败全部释放，按间隔重试至资源等待超时（FAIL: resource_busy）。所有调度 Agent 实例共享一个 locks 目录，进程退出 OS 自动解锁。
3. 拿锁后还要检查物理 NPU 上其他用户进程；只操作本任务的 PID/进程组，不清理 auto-occupy/外部进程。检查与启动之间仍存在外部 TOCTOU，不能宣称可完全避免。
4. 调度任务状态 `seen → waiting_resources → staged → running → pending_report → reported` 持久化；不可恢复中断明确标记并告警，重复回传按 run_id/attempt 幂等处理。
5. GitHub 取消、节点超时/失败，应只停止**本任务**其它节点进程组并释放锁；不能留悬挂多节点 torchrun。
6. 新 agent 切换前先停旧 8P/16P 各自 Agent，防止同 Run 被双重认领；回滚保留 state 和日志。

## 场景配置
- `lite-actions/config/pools.json`：A3/A5 节点 SSH、内网 IP、物理 NPU ID、网络接口、Ascend toolkit、资源互斥域；A5 64P 为 8×8 模拟拓扑。
- `lite-actions/config/pipelines.json`：工作流 ID、测试入口、资源池、设备/节点需求、执行和等待超时；不接受 GitHub 任意 shell。
- `OverrideDefinitions`（测试仓）管理训练 env_vars、train_args、ngpu、expected_steps、timeout；多节点适配提供 per-node NGPU 8 和总 world_size=16/64，按节点并发 launch 并在 master verify。
- A3 16P Eager smoke 保留 AdamW（已知 Muon bucket-plan 问题）；A3 8P 使用自身 example 配置，不允许迁移时盲改 optimizer。
- A5 64P 首次接入最多新建资源池、用例定义、可选 GitHub 薄工作流 3 个入口；不新增 Agent/Dispatcher/Waiter。A5 Flash 原脚本默认 EP128/DP_SHARD128 不适配 64P，须显式合法覆盖并验证 TP×CP×PP×DP 与 EP 并行约束。

## GitHub/执行生命周期
`workflow_dispatch → GitHub group 队列 → Waiter → Agent 主动轮询工作流列表 → 可信 Run/Commit SHA 校验 → 物理资源原子预留 → NPU 预检查 → git archive 固定 SHA → SSH staging（节点独立目录）→ N 节点并发启动 → 节点结果汇总 → master TensorBoard 校验 → 有界日志裁剪 → Commit Comment → Waiter 输出最终 PASS/FAIL`。

GitHub POST Body ≤7000 B，日志最多 20 行×180 字符，程序统计 HTTP 请求行+头+Body（不含代理/TLS/空闲轮询）；执行全日志和快照留在执行机，调度机记录北京时间 `YYYY-MM-DD_HH-MM-SS_run-ID_attempt-N/`、upload-metrics.json。

## 开发/验证/交付
| 案例 | 验收条件 |
| --- | --- |
| YAML | A3 8P/16P 各 `queue:max`，不取消运行中任务，统一 Waiter |
| Mock 同用例 | 连点两次：第一个执行、第二个 Pending（无丢弃） |
| Mock 互斥 | 同物理设备第二任务等待；不同芯片可并发；部分取锁失败自动回滚 |
| Mock N 节点 | 2/8 节点 stage/launch/退出码；失败停止自己子进程 |
| Mock 失败 | 超时、占用、网络 POST 重试、取消均非假 PASS |
| 8P E2E | GitHub 手工触发 8P、5 step eager；成功/日志/上传计量 |
| 16P E2E | GitHub 手工触发两机各 8 NPU、EP16 eager、TensorBoard 验证及日志 |
| A5 64P | 8×8 资源拓扑/并行参数/Mock 通过即可，绝不对真实设备运行 |

操作顺序：从 PR #26 建独立工作树和分支、落地单 Agent/资源锁/通用执行器及测试；逐步切换旧 Agent 并实际 Actions 验证；失败回滚旧实例与分支。 `master` 可能被 sync-upstream 强制覆盖，须合入 GitCode 或使用 CI overlay 保持配置。
