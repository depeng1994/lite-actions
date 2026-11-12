# 测试范围与新增门槛

新增测试前先回答：它要阻止哪一种用户可见的端到端失败？已有场景为什么不能发现？能否扩展已有场景？如果只能证明某个配置字符串、字段排列或开发过程中的快照没有变化，不新增用例。

CPU 测试运行入口：

```bash
python3 -m unittest discover -s tests
```

`tests/test_workflow.py` 从序列化的请求 Artifact 开始，实际经过下载解码、schema 3 校验、固定源码定义核验、资源锁、调度状态和结果回写。GitHub 服务与模型执行是明确的外部替身，不代表真实 HCCL 或训练通过。测试使用独立的虚拟节点和临时目录，不读取生产 `pools.json`，也不锁定生产 IP、容器名或配置布局。

| 场景 | 必要性与最终观察 |
| --- | --- |
| 独立 worker 接单与去重 | 同一计划只由指定 worker 执行，每 case 只回写一次 |
| 同机并行 | 实际持有互斥设备锁与不同端口，完成后能够重新分配 |
| 分布式队首阻塞 | 等待时不留下部分租约，不让后续小任务插队；资源释放后完成 |
| 节点执行能力 | 资产不齐全的节点不接训练，仍可参加双节点通信 smoke |
| 外部占卡 | 等待或改选其他完整分配，不隔离健康资源、不回收其他任务 |
| 重跑与运行中取消 | 旧 attempt 不执行，取消结果在清理完成后回写 |
| 上报断网与重启 | 恢复已完成结果，不重复训练、不重复发送结果 |
| 发布目录更换与管理操作并发 | 更换代码副本仍恢复同一份待报结果；管理进程写入隔离记录时，接单到结果回写不会丢任务或隔离。复用上报恢复场景，只新增一个真实进程并发场景 |
| 执行中崩溃 | 保留隔离，不盲目重跑或复用仍可能被占用的设备 |
| 日志预算 | Yellow 10 行/2,048 B；Blue 按 4 MiB 计、不按行计；身份和实际发送字节分别验证 |
| 成功/失败诊断 | 成功结果实际经过 LOSS 选择后回写；失败结果保留根因报错，避免错误被收尾噪声覆盖。后者是必要的独立失败路径，不能由成功预算场景替代 |

训练成功的摘录必须包含 LOSS，优先 rank 0；失败时选择关键报错和 traceback 上下文。过长前缀不能截掉 LOSS 数值或异常消息。通信 smoke 不训练模型，因此返回 collective 校验结果而非伪造 LOSS。训练没有 LOSS 证据时，不报告 PASS。

`python3 scripts/lint.py` 同时执行 Ruff 与 Device ID 单行检查；该格式约束在 lint/CI 中验证，不新增配置快照单测。

`tests/test_process_lifecycle.py` 在 Linux/root 上使用真实 supervisor、CPU 子进程和 `/proc`：启动带任务标记的分离子进程，拒绝提前释放，取消后验证全部已退出，同时保留无关进程。它在 blue worker 执行；本地 macOS 和 yellow 非 root 环境跳过，不将跳过写为通过。

真实验收使用模型仓现有入口：系统探测、8P/16P HCCL、DSV4 Muon/AdamW 五步完成性，以及并发、取消和资源复用。CPU 测试不能替代这些检查；实际状态见 [验证记录](validation.md)。


## Auto Update（RFC #19）

新 DT：tests/test_auto_update_check_source.py、test_auto_update_version_switch.py、test_auto_update_recovery.py、test_auto_update_integration.py、test_worker_ready.py；覆盖 14 项精简 AU 核心场景。自动更新器不根据部署区域选择行为。所有 Git fixture 均为临时本地仓库，systemctl 在 DT 中使用 mock；没有生产 NPU 操作。

E1/E2：隔离 Linux 的真实 systemd Timer/Service A→B、NO_CHANGE、停止运行中任务、启动失败回滚和一次事务中断恢复。验证新 Worker 请求启动/进程出现/Ready 的 5/30/120 秒设计目标。

E3/E4：选定真实 Worker 的首次一键安装、自动切换、CPU-only Worker OS Probe、结果回写及再次接单。当前阶段为避免启动昂贵资源，Dispatch 验证输入仅选择 CPU Probe，**8P 和 16P 保持 NOT_TESTED**；不得改变主线正式 Dispatch 默认用例集或将 CPU 结果冒充 NPU 验证。若目标 Remote 容器没有 systemd，真实部署 E2E 必须标记 BLOCKED，不得用 CPU DT 抵消。


<a id="rfc24"></a>
## RFC 24：候选分支验证与生产前置条件

**不要直接安装替换线上。** 基础设施 fixed probe、区外上限、三仓 E2E 是不同的验收门槛。托管 runner 的 `uname`、成功的模板解析、非生产机上的单测都不等于 Yellow probe 或 NPU smoke。

1. 无外网/生产凭据的开发 CPU 环境中，从每个模型仓受测源码导出 `.ci/lite-actions.json`，用相同 suite 参数重新运行 `python3 /path/to/lite-actions/scripts/export_case_catalog.py --repo-root . --check`。TorchTitan 的 native 分支加 `--suite dispatch_smoke --suite base --suite pr --suite nightly`；保留重复 8P。导出器会运行模型代码，**绝不能在 Worker 或持有生产凭据的 job 中执行**。无清单的旧 SHA 不能回退执行。依赖尚未配置的训练项可发现，但执行时仍必须满足必需资源/环境条件。
2. 受信运维管理员准备 root 所有、不可由组/其他用户写的 `/etc/lite-actions/deployment.json`。顶层 `addresses` 放两条经批准的本地网卡 IPv4（不是 NAT 出口）；`workers.<zone>` 包含 `state_dir`、`case_environments`、可选 `probe_callers`。Yellow 另须 `daily_upload_bytes`（UTC 日窗口，经运维确认）及 `artifact_hosts`（仅精确核准的无凭据下载域名）。`case_environments` 按环境名、环境变量键映射到允许运行的值列表；不可从 PR 扩充。状态目录必须预先创建且权限受控，内有独立 `worker-id` 标记；切换旧 outbox 必须核实归属并排空冲突。无固定地址/身份不符时拒绝启动，不允许在测试容器改 IP 或复制凭据冒充生产节点。`--validate` 也执行身份预检。
3. 目前 GitHub probe 只接受显式授权的 `workflow_dispatch`：在 `probe_callers` 中登记 `repo`、准确 `workflow` 路径、`branch`、`approved_caller_shas`、`approved_template_shas` 及 `mode`（`cpu-only` / `mixed`）。模型仓变量 `LITE_CI_RESULT_ACTOR` 须匹配现有可信发布者。Worker 从平台原 run/attempt 的 `referenced_workflows` 与活动 job ID 独立核验，不由请求自报身份。GitCode 的可复用 YAML 是**先决 marker 试点**，缺独立 callee/dispatch 证据就退出非零；当前分支不宣称 GitCode 的主机 probe 已实现或验通。
4. Blue 公开脱敏日志默认关闭。启用须登记 `public_release` 下 `public_diagnostics_approved`（true）、`repository`、已存在且非 draft 的 `release_id`、`tag` 和 `publisher_login`；写身份从既有受控 `LITE_ACTIONS_BLUE_RELEASE_TOKEN` 注入。不要创建临时放宽权限的 Release/Token 来测试。模型配置 `LITE_CI_PUBLIC_LOG_REPO` 与实际目的地一致；普通读者无须登录。每个 case 先占用持久额度再发送，一份文件、字节数和 SHA-256；不明回执先查重，只清理有自己回执且身份/摘要一致的过期文件。日志是否成功投递和用例是否通过分别报告。
5. 在固定分支 SHA 跑 `python3 scripts/lint.py`、`python3 -m unittest discover -s tests -v`。`tests.test_rfc24_boundaries` 覆盖真实 loopback 正对照/无模型执行、安全边界及原生 Release 协议的假后端；`tests.test_workflow.Rfc24RecoveryTests` 用真路由与状态文件、虚拟时钟和假平台验证丢回执/断线/重启，并检查模型代码未在到达执行边界前运行。记录跳过项，不以 mock 代真机。
6. E1/E2/E5 核心必跑；E5 只做一次三仓回归：TT-GC 组织仓 Dispatcher Smoke、vLLM-GH 完整 Dispatcher Smoke、Lite Actions 同部署 SHA 的 `profile=full`。不跑 Nightly 权重训练；核对每个必需实例、真实资源、exit=0、cleanup_verified=true 和日志位置。TT-GC 合入及其现有 Actions 同步证据另记，不对 TT-GH 写入。E3 真机验收必须匿名读回 4 MiB 合成文件核对哈希；E4 实际出口计量和 ACK 时效需独立线上样本，虚拟时钟不得当作生产网络证据。
