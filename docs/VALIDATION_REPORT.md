# A3/A5 CI 重构验收报告（2026-10-09）

## 交付与版本

- GitHub 仓库：[lite-actions](https://github.com/depeng1994/lite-actions)，`main` 为重构前快照 `1a7bcc1`，`refactor/unified-a3-a5-ci` 为重构分支。
- 模型仓：`depeng1994/torchtitan-npu` 的 `refactor/unified-a3-a5-ci` 分支；E2E 源码固定到 `e6304661b8a265916e92b617a6f03a1a68542ef4`。
- 新架构：GitHub `queue:max` + 唯一 Waiter；内网统一 Pull Agent（无需入站）；跨进程物理 chip 文件锁；A3 8P/16P 受信任执行适配器；定量上传结果。
- A5 64P：禁用的 8×8 假资源池；模型定义 A5 Pro `EP=32`、`DP_SHARD=32`、`DP_REPLICATE=2`（候选）；仅 CPU 测试，**未做 A5 硬件训练**。

## 真实 Actions 证据

| 用例 | GitHub Run | 结果 | 证据 |
| --- | --- | --- | --- |
| A3 8P | [37868718990](https://github.com/depeng1994/torchtitan-npu/actions/runs/37868718990) | **PASS** | 单机 8 NPU、Eager、5 steps，GitHub Job Success，上传 20 行指标 |
| A3 16P | [37868722207](https://github.com/depeng1994/torchtitan-npu/actions/runs/37868722207) | **PASS** | 两机各 8 NPU、EP16、AdamW Eager、5 steps，TensorBoard 验证通过，GitHub Job Success |
| 同 Workflow 排队 | [37869377044](https://github.com/depeng1994/torchtitan-npu/actions/runs/37869377044) | **已验证** | 首个 8P 运行时第二次触发先进入 `queued`；首个结束后才进入 `in_progress`；主动取消以免重复训练，确认未被内网 Agent 领取 |

## 资源/流量验证

- 首次 8P 与 16P 同时执行时，A3-4 chip 0（8P）及 chip 8（16P）、A3-3 chip 0（16P）的文件锁均验证为 `LOCKED`；任务完成后释放。
- 两个资源集合在同一物理机 A3-4 上不重叠，实际并发调度成功；重叠互斥、部分锁回滚另有跨进程 CPU 单测。
- 8P 单次 POST Body **2332 B**、任务阶段 HTTP 请求估算 **2984 B**；16P 分别 **1281 B** / **1933 B**。估算不含长期空闲轮询、代理、TLS/TCP。
- E2E 日志分别在 `logs/runs/2026-10-09_09-13-26_run-37868718990_attempt-1/` 和 `...09-13-28_run-37868722207_attempt-1/`。
- 执行机完整训练日志留在 `/mnt/share/ci_tests/<本次任务目录>/`，仅有界结果回传 GitHub。

## 测试与遗留限制

- `lite-actions`：17 项 CPU 单元测试 + 4 项原有单元测试，通过；包括资源竞争、NPU 解析、信任校验、Waiter 协议和 `queue:max` 静态检查。
- `torchtitan-npu`：A5 64P 3 项配置/拓扑测试通过。
- **尚未解决**：远端进程在 Action 取消/网络断开后的强一致清理；Agent 重启时 `running` 状态需要人工确认；共享环境的外部任务不遵守本 CI 的锁；`master` 上的专属 Workflow 可能被 GitCode 强制同步覆盖。
- **2026-10-09 正式切换**：旧 2P/8P/16P Agent 进程均已停止，统一 Agent 已迁至 `lite-actions/main` 实际接管（受信任 master / 验证分支）。旧 Agent/Waiter 源码已从本仓移除，历史日志与状态保留。
- 8P/16P 的模型专用 launch/verify 适配器暂保留；本轮统一的是 GitHub 控制面、资源管理、执行调用接口和日志回传。更彻底的 N 节点 SSH 传输抽象可以后续独立重构。

## 执行机广播（2026-10-09）

- 调度机不广播；Dispatcher 在确认资源空闲并持有物理 NPU 锁后，通过 `wall -n` 广播到本次执行机**宿主机及训练容器**，使用北京时间、88 列星号边框和动态 GitHub Run URL。
- 真实 A3 8P [Run 37876792907](https://github.com/depeng1994/torchtitan-npu/actions/runs/37876792907)：5-step Eager 训练结束，GitHub Job **Success**；10:55:29–30 开始广播、11:02:45–46 结束广播均成功发送到 `a3-4-relay` 宿主机及 `a3-4-docker-relay` 容器。资源锁释放，Agent 最终状态 `reported/PASS`。
- A3 16P 本次预检查发现 A3-3 外部任务占用，因此未触发新一轮训练，也**未发送误导性占用通知**。
- 本版本的广播格式、失败拒绝启动、退出后资源复查、未释放时改发警告通过 CPU Mock；2026-10-09 共 23 项单测通过。
- 本次 8P 验证上传计量：POST Body 2410 B，任务阶段 HTTP 应用层上传约 3062 B；终端广播通过内网 SSH，不计入 GitHub 上传额度。
