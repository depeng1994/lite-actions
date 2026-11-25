# Lite Actions Project Guide (`AGENTS.md`)

## 核心红线（总纲）

1. **禁止擅自添加不必要的安全拦截校验**：
   > 不要为了所谓的安全，加各种安全拦截校验（包括不限于白名单 Action、白名单用例、以及拦截各种 SHA 校验不一致），这只会引入额外的工作量，除非你跟我确认过这是必要必须加的安全校验。

2. **简洁与工程实用优先**：
   - `Router.eligible()` 只负责按工作流名称前缀（`lite-ci-`）、运行状态（`queued` / `in_progress`）以及测试隔离（如 TestWorker 不抢占主线 `lite-ci-self-smoke`）过滤，不再维护事件类型（`workflow_dispatch` / `issue_comment` / `pull_request_review` 等）或分支/SHA 白名单。
   - 新增或修改任何控制面、触发器或状态上报逻辑时，避免引入未与维护者确认的隐式白名单或额外拦截层。

## 开发与验证规范

- **统一使用 Docker 镜像运行静态检查与单元测试**：
  ```bash
  docker run --rm --init -v "$PWD:/workspace" -w /workspace lite-actions-test:py312 \
    sh -c "ruff format --check && ruff check && python3 -B scripts/lint.py && python3 -B -m unittest discover -s tests"
  ```
- 代码、注释、测试和提交说明中保持简洁直观，不引入内部计划编号或过程性标签。
