# vLLM Blue Smoke：OS / 8P / 16P 生产验收

## 本次完成的范围

vLLM GitHub 原生个人 Fork PR → 可信 GitHub 控制代码 → 单一 Blue Worker → GitHub 结果，默认 Blue PR 套件覆盖 **OS、单机 8P、双机 16P**。该范围已经实际部署并验证；它不是整个 TorchTitan GitCode Forward 项目的完成声明。

验收时间（UTC）：`2026-11-02T01:01:15.585098+00:00`。

| 项目 | 不可变发布 |
|---|---|
| 生产 Blue Worker | `f4b15e32b8348d0762cb808a85f7ea0a6f33745a` |
| 生产模型控制 `cann-framework/vllm-ascend:main` | `cc5caeaf08b2e29b3dc41aced0ed023083dd7899` |
| 模型个人开发 `depeng1994/vllm-ascend:gitcode-forward` | `05216f3424186f2068e8b9a16de8ed021ad1e330` |
| 模型控制代码测试 tree | `2b7e8615dce1a718c3bdf028c7f96dd20d8b2550` |
| Worker 开发分支 | `depeng1994/lite-actions:gitcode-forward`；后续文档提交不改变上述部署 SHA |

## 默认覆盖与操作入口

个人 Fork PR 加 `lite-a3-blue` 标签即默认运行以下三项；**不再需要 `lite-a3-16p` 标签**。旧标签不改变规格、不增加重复 Case。Prepare、版本检查、Base 更新补偿与 Worker 独立准入均要求相同完整套件；旧 OS+8P-only 结果不能继续作为当前完整套件通过。

| Case | Worker | 节点 × 设备 | 容器 | 通过依据 |
|---|---|---|---|---|
| `vllm_worker_os_blue` | Blue | 0 × 0 | 无 | Worker OS 探测，不启动 Docker，不占 NPU |
| `dispatch_smoke_a3_8p` | Blue | 1 × 8 | `vllm_hccl_smoke` | 真实 HCCL，WORLD_SIZE=8，SUM=28，退出 0，清理确认 |
| `dispatch_smoke_a3_16p` | Blue | 2 × 8 | 两节点各自 `vllm_hccl_smoke` | 真实 HCCL，WORLD_SIZE=16，SUM=120，两节点退出与清理确认 |

GitHub `Dispatcher-Smoke` 手动执行完整 Blue 套件的 `test_cases`：

```json
[{"test_id":"vllm_worker_os_blue"},{"test_id":"dispatch_smoke_a3_8p"},{"test_id":"dispatch_smoke_a3_16p"}]
```

保留显式手动单项/子集调试。为兼容原 Yellow 工作流，**空手动输入仍选择旧 A5 8P**；未加 Blue 标签的旧 PR 路径与 Yellow A5 默认值没有被本次覆盖。手动子集成功只代表所选子集，不作为完整 Blue 套件验收。

## 真实 GitHub 运行证据

| 场景 | Run / Attempt | 实际结果 |
|---|---|---|
| 生产控制手动 Blue OS+8P+16P | [41368818404 / 1](https://github.com/cann-framework/vllm-ascend/actions/runs/41368818404/attempts/1) | 三项 PASS，汇总 SUCCESS，全部清理确认 |
| 个人 PR 默认三项，恢复真实载荷 | [41369868409 / 1](https://github.com/cann-framework/vllm-ascend/actions/runs/41369868409/attempts/1) | 仅 `lite-a3-blue` 标签；三项 PASS，汇总 SUCCESS，实际执行合并结果 |
| 个人 PR 故意失败载荷 | [41368985668 / 1](https://github.com/cann-framework/vllm-ascend/actions/runs/41368985668/attempts/1) | OS PASS；8P、16P 均真实 FAIL；汇总 FAILURE；失败进程清理确认 |
| 同一新版 Worker 的 TorchTitan 兼容回归 | [41368844204 / 1](https://github.com/cann-framework/torchtitan-npu/actions/runs/41368844204/attempts/1) | OS、8P、16P 全 PASS，汇总 SUCCESS，全部清理确认 |

失败场景的两个 NPU Case 均在各自实际执行日志中确认 `EXPECTED_BLUE_ACCEPTANCE_PAYLOAD_FAILURE_20261102`，不是错误 SHA 或 Prepare 拒绝替代的假负向测试。随后恢复正常 SUM all-reduce 载荷，再以新 PR Head 跑通默认三项。验收 PR #4 保持 open/unmerged，不将其故意失败提交合入生产。

### PR 正向验收身份

- Head H：`13b02cd5f40cbe999f3968ba3904dff2255947c5`。
- 最新 Base B：`cc5caeaf08b2e29b3dc41aced0ed023083dd7899`。
- 实际执行合并提交 M：`033eb6b8e111bbb6bd6abb3bdba59526dfc98b13`。
- 合并树 T：`20e0d62a1f8b6d28cb16016d3a4aea938714d4a4`。
- 控制提交 C：`cc5caeaf08b2e29b3dc41aced0ed023083dd7899`。

三项 Case 的 source metadata 一致；验收重读 PR、当前 Base 与 M 的父提交，确认 `parents(M)=[B,H]`、tree=T、M≠H。8P/16P 的容器内 JSON 证据记录相同 source_sha 与实际 payload_path，不以历史源码或仅 Head 的结果替代。

## 容器、清理与生产切换

两节点均按不可变容器 ID 绑定实际 `vllm_hccl_smoke`，不回退到 TorchTitan 容器：

| 节点端口 | 容器 ID |
|---|---|
| 153 | `9128bc83d0e2314669597e9db63a327b0809860df617c0f8fe968559f63851fc` |
| 154 | `c33953f779ae16fdf46b5db195305cf0aed4febf30fd13bf2dfa0831c0301421` |

镜像 ID：`sha256:f81de6249765738a06b5c187dcf0968dfd0ce828e8b6b16a2771de2b0e78217c`。Name/ID/镜像/运行状态、真实硬件结果及清理证据按 Case 关联；两个容器共享宿主机物理设备与端口租约，采集到的跨仓执行快照没有重复占卡。

生产采用 drain → 快照 → 停旧实例 → 切换不可变 release → 验证 → 启动。82 条切换前任务状态保留，原 :153 设备 0–7 隔离对象保留；state/config/log/cache 持久化链接保留。pool/workers 文件未改变；服务环境只将可信 vLLM 控制 SHA 绑定到本次发布。没有重置任务数据库，没有删隔离，没有修改 Yellow、Required Checks 或分支保护。

本次同时部署已提交的原生任务目录修复：GitHub 长 PR 显示名不再作为执行目录身份，使用严格 Run ID/Attempt/Case；旧 TorchTitan/A5 目录规则不变。

## CPU 与跨仓合同验证

精确 Worker 提交：196 项单测全部通过，lint 通过，56 个 Python 文件格式通过。精确模型提交：34 项相关控制测试通过，Ruff/compile 与两个 Lite Actions Workflow 的 actionlint 通过。额外将实际模型 Producer 生成的三项 Plan 交给实际 Worker 解析与版本校验，完整套件通过、删掉 16P 被拒绝。

机器记录、原始日志摘要、逐 Case 容器/源码/清理证据保存在 [机器可读验收记录](evidence/vllm-blue-os-8p-16p-production.json)；原始日志位于授权控制机 `/root/gitcode-forward-work/` 与 `/var/log/lite-actions/blue/runs/`。

## 已知边界与尚未关闭的工作

测试状态采用 `versioned_only`：绑定已测 H/B/T；`merge_gate=NOT_PROVIDED`，不声称平台原子合入许可，也不修改 Required Checks。

控制发布后自动 Base 补偿 Run `41368791253` 因 GitHub 预合并提交仍指向旧 Base 被安全拒绝，零硬件执行。本轮随后更新验收 Head 并刷新 PR 目标元数据，才完成新 Head 的合并测试。**这不构成该 Base-only 缓存竞争场景无需人工补偿即可恢复的实证**，该项应单列继续修复/验收，不能用本次正向 PASS 覆盖。

本次没有完成整个 TorchTitan GitCode Bridge 的跨平台发布/App/事件回写及所有 E2E/INT，也没有进行 vLLM 完整推理、Yellow A5/32P/64P 或硬件 Worker 崩溃重启验收。以上未验收内容不因本次 Blue 三项成功而变成 PASS。
