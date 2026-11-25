# Lite Actions Project Guide (`AGENTS.md`)

## 核心红线（总纲）

1. **禁止擅自添加不必要的安全拦截校验**：
   > 不要为了所谓的安全，加各种安全拦截校验（包括不限于白名单 Action、白名单用例、以及拦截各种 SHA 校验不一致），这只会引入额外的工作量，除非你跟我确认过这是必要必须加的安全校验。

2. **提交 PR 前严格清理冗余测试，只保留端到端故障模式 ST 用例**：
   - 提交代码到 PR 前，务必检查所有待提交的测试代码：凡是冗余的、面向开发过程的、写死各种无意义校验（例如逐字段 getter/拼接校验、YAML/代码文本子串匹配、无真实故障背景的枚举断言）的用例，**一律删除**。
   - 真正要看护的测试，是**面向端到端业务场景中的真实故障模式设计的系统测试（ST）**——明确对应一种真实会发生的业务故障，从端到端入口驱动并断言故障被拦截或正确恢复。
   - **仓库内参考范例**：[`tests/test_dispatch_smoke_st.py`](tests/test_dispatch_smoke_st.py)
     - ST-1：多机 16P 跨节点 HCCL 握手防误选本地 Docker 网桥导致 `EI0015` 卡死
     - ST-2：外容器 NPU 进程名称为空时容器清理校验不误判残留
     - ST-3：`prepare -> report -> wait` 端到端进度流式回显与脱敏日志尾部展示
     - ST-4：黄区 TestWorker Broker 多仓 ACK 与 tick 状态流转
     - ST-6：并发清理触发 SSH `255` 时自动重试并完整暴露 `revoke` 错误明细

3. **简洁与工程实用优先**：
   - `Router.eligible()` 只负责按工作流名称前缀（`lite-ci-`）、运行状态（`queued` / `in_progress`）以及测试隔离（如 TestWorker 不抢占主线 `lite-ci-self-smoke`）过滤，不再维护事件类型（`workflow_dispatch` / `issue_comment` / `pull_request_review` 等）或分支/SHA 白名单。
   - 新增或修改任何控制面、触发器或状态上报逻辑时，避免引入未与维护者确认的隐式白名单或额外拦截层。

## 开发与验证规范

- **统一使用 Docker 镜像运行静态检查与单元测试**：
  ```bash
  docker run --rm --init -v "$PWD:/workspace" -w /workspace lite-actions-test:py312 \
    sh -c "ruff format --check && ruff check && python3 -B scripts/lint.py && python3 -B -m unittest discover -s tests"
  ```
- 代码、注释、测试和提交说明中保持简洁直观，不引入内部计划编号或过程性标签。
