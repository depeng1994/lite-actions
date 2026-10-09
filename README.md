# Lite Actions

**机制**：GitHub Action 手动触发、`queue: max` 串行排队；内网唯一 Pull Agent 主动轮询 GitHub，按宿主机物理 NPU 锁调度 2P/8P/16P，使用 SSH 执行固定测试，最终仅回传 PASS/FAIL 和最多 20 行日志。无需入站端口或 Self-hosted Runner。

- **执行机广播**：获取资源锁且确认 NPU 空闲后，通过 SSH 在**参与本次任务的宿主机与训练容器**运行 `wall -n`；开始与释放时均打印 88 列星号边框、北京时间、警示和当次 GitHub Action 链接；**调度机不广播**。广播失败不启动训练；退出时若占用未清空则广播“仍可能占用，请勿使用”，不得虚报已释放。
- **配置**：`config/pools.json` 管节点、物理 NPU、宿主机/容器广播 SSH 别名；`config/pipelines.json` 管可信 Action、资源、超时；`ci_core/` 管锁、执行与 GitHub API；`github/workflows/` 和 `github/wait_result.py` 同步到模型仓供 GitHub 执行。
- **启动**：`nohup ./run-unified-agent.sh >/dev/null 2>&1 < /dev/null &`；默认仅处理 `master` 上维护者触发的 Workflow，验证分支通过 `A3_CI_ALLOWED_BRANCHES` 显式授权。文件锁防多开，容器重启不会自动恢复。
- **测试**：`python3 -m unittest discover -s tests`；`python3 unified_agent.py --validate`。A5 64P 为**禁用**的 8×8 配置，仅做 Mock。
- **日志**：`logs/runs/YYYY-MM-DD_HH-MM-SS_run-ID_attempt-N/`（北京时间）记录任务日志/上传计量；`logs/system/` 记录 Agent；`state/` 记录去重/文件锁。仅清理过期的 `logs/runs/`。
- **限制**：应用层 POST Body ≤7 KB，统计不含 TLS/代理及空闲轮询；物理 NPU 检查无法阻止 CI 外部用户同时起任务；GitHub Commit Comment 公开可见，勿上传敏感信息。未解决的取消与异常重启恢复见 `docs/CI_REFACTOR_DESIGN.md`。

`legacy/` **仅保留仍需调用的远程执行/日志解析适配器**，不含旧 Agent/Waiter。
