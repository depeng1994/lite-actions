# A3 CI（2P / 8P）

**机制**：GitHub Action 手工触发并等待；内网 Agent 每 60 秒主动轮询 GitHub，执行预设流水线，将 PASS/FAIL 和最多 20 行日志写入 Commit Comment，Action 读取并判定结果。**无需入站网络或 Self-hosted Runner**。

- **2P**：`agent.py` → `pipeline.sh` → 本机 `npu-smi info`（仅连通性 PoC）；回传末尾 20 行。
- **8P**：`../torchtitan-npu-a3-8p-ci/.github/scripts/a3-8p-agent.py` → 打包指定 commit → SSH `a3-4-docker-relay` → DeepSeek-V4 Flash 8P 训练（eager、5 steps）；完整日志留执行机 `/mnt/share/ci_tests/<任务名>/`。PASS 回传前 20 行 TPS/耗时指标；FAIL 优先报错栈与末尾日志（合计 ≤20 行）。
- **启动**（本目录）：2P：`nohup ./run-agent.sh >/dev/null 2>&1 < /dev/null &`；8P：`nohup ../torchtitan-npu-a3-8p-ci/.github/scripts/a3-8p-start-agent.sh >/dev/null 2>&1 < /dev/null &`。各自防多开，**容器重启不自启**。
- **触发**：[GitHub Actions](https://github.com/depeng1994/torchtitan-npu/actions) → A3-2p-CI / A3-8p-CI → Run workflow（`master`）。**8P 仍在 PR #26，尚未合入 master**。
- **依赖**：Python 3、GitHub 出站 HTTPS、`gh auth login`（评论写权限）或 `A3_CI_GITHUB_TOKEN` / `A3_8P_CI_GITHUB_TOKEN`；8P 另需 SSH 与执行机训练环境。
- **日志**：2P：`logs/`；8P：`a3-8p/logs/`。`runs/YYYY-MM-DD_HH-MM-SS_run-ID_attempt-N/`（北京时间）存任务日志和上传计量；`system/` 为 Agent 日志，`state/processed.json` 防重。只清理旧的 `runs/`。
- **维护**：2P 测试 `python3 -m unittest -q test_agent`、`python3 agent.py --smoke`；单次回传 Body ≤7 KB，统计不含 TLS/代理与空闲轮询。**公开 Commit Comment 勿含敏感数据**；GitCode 强制同步须保留 GitHub 专属 `.github/` 文件。
