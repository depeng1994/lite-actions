# Lite Actions

**机制**：GitHub Action 手工触发并串行排队（`queue: max`）；内网单 Agent 主动 GET 领取可信任务，按物理 NPU 锁调度本地/SSH 多机流水线；训练日志留执行机，仅 POST PASS/FAIL + 最多 20 行诊断。无需公网入站或 Self-hosted Runner。

- **入口**：`github/workflows/`（2P NPU 连通性、8P 训练、16P 双机训练）；模型仓需部署对应 YAML 与 `github/wait_result.py`，本仓是调度逻辑主源。
- **核心**：`unified_agent.py`（轮询/去重/回传）、`ci_core/resources.py`（跨进程芯片锁）、`ci_core/dispatch.py`（受信任执行器）、`config/`（A3/A5 资源池及用例映射）。
- **启停**：`nohup ./run-unified-agent.sh >/dev/null 2>&1 < /dev/null &`；先停旧 2P/8P/16P Agent，避免同任务重复领取。默认仅接收 `master` 的维护者手工任务；测试分支由 `A3_CI_ALLOWED_BRANCHES` 显式授权。容器重启不自启。
- **测试**：`python3 -m unittest discover -s tests`；`python3 -m unittest test_agent`；`python3 unified_agent.py --validate`。A5 64P 为禁用的 8×8 Mock 配置，不执行训练。
- **数据**：`logs/runs/YYYY-MM-DD_HH-MM-SS_run-ID_attempt-N/`（北京时间）存执行日志/上传计量，`state/` 存去重与锁；清理时只删除旧的 `logs/runs/`。
- **约束**：同设备串行、互不重叠允许并行；不抢占其他人的 NPU。物理占用检查无法消除外部用户启动任务的竞争。上传 Body ≤7 KB，传输统计不含空闲轮询/TLS；GitHub Commit Comment 公开可见，勿上传敏感数据。
- **待完善**：取消任务后跨节点远端进程的强一致清理、异常重启恢复、模型仓工作流随上游同步的保留策略。详见 `docs/CI_REFACTOR_DESIGN.md`。

`legacy/` 保留旧 Agent/Waiter 快照；Dispatcher 暂用其中 8P/16P 远程流水线适配脚本。**不要同时启动旧 Agent 与统一 Agent 监听相同 Workflow**。
