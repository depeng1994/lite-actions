# Lite Actions

Lite Actions 让模型仓在 **GitHub Actions 或 GitCode Actions 上发起测试，在自己的机器上执行**。模型仓决定“测哪版代码、跑哪些用例”；Worker 主动拉取任务，决定“在哪些节点、设备和容器上跑”，再把结果回写给原工作流。外部平台不需要连入 Worker 所在网络。

它适合三类任务：在 Worker 本机做不占卡的 **CPU/OS 探测**、在一个或多个节点上做 **NPU 通信冒烟测试**、以及执行模型仓定义的**训练用例**。只想使用已经接入的集群，不需要自己安装 Worker。

## 第一次使用：先跑一个不占卡的用例

**已有 Worker，想验证整条链路：** 使用有权触发工作流的 GitHub 账号，在本仓的 **Actions → lite-ci-self-smoke → Run workflow** 中选择已部署的分支和 `profile=cpu-only`。也可以用已登录的 GitHub CLI：

```bash
gh workflow run lite_actions_self_smoke.yml \
  --repo depeng1994/lite-actions --ref main -f profile=cpu-only
```

该命令只运行 `worker_os_blue`。在本次运行的 `smoke` 作业中应看到用例 `PASS`；受信账号的提交回写应包含 `exit_code=0`、`cleanup_verified=true` 和零设备请求。[查看一次成功运行](https://github.com/depeng1994/lite-actions/actions/runs/43011224684)。**不要省略 `profile=cpu-only`**：默认 `full` 还包含 8P/16P 通信用例。自检要求 Worker 正在运行所选分支的确切提交；刚合入 `main` 时，应先确认 Worker 已更新。

**想验证模型仓，而不是本仓自检：** 在已接入的 `vllm-ascend` 上，以下命令只选同一个 OS 探测。这里的输入名是模型仓定义的 `test_case`，不是本仓的 `profile`：

```bash
gh workflow run lite-ci-dispatcher-smoke.yml \
  --repo cann-framework/vllm-ascend --ref main -f test_case=worker_os_blue
```

[跨仓运行示例](https://github.com/cann-framework/vllm-ascend/actions/runs/43012149635)中，`prepare`、单用例作业和汇总作业 `Lite-Actions-Smoke` 都成功，才算这次调度完成。CPU 探测验证接单、执行和回写，不验证 NPU 通信或模型精度。

## 任务如何从模型仓到达设备

```text
模型仓：开发者触发 Actions → 工作流固定源码提交并准备测试计划
                                      ↓ Worker 主动拉取
Worker：核对仓库、SHA、运行批次和用例 → 只接属于自己的任务
                                      ├─ OS 探测：在 Worker 本机执行，不申请设备
                                      └─ NPU 用例：按资源池排队 → 锁定整组设备和端口
                                                    → 经 SSH 进入已绑定的容器执行
执行结束：核验清理结果 → 回写用例状态和诊断摘要 → 原工作流汇总
```

- **模型仓维护者**负责工作流、用例和执行入口。GitHub 路径在配置的仓库中发现**工作流名称**以 `lite-ci-` 开头的运行，不限于手动或 PR 触发；名称不是授权，也不能代替测试计划。常规模型工作流需提供与本次运行及重试次数绑定的请求和源码凭据（`lite-ci-request-<attempt>`、`lite-ci-source-<attempt>` 两个制品）。本仓自检使用独立的 SHA 校验。可参照 [vllm-ascend 的工作流](https://github.com/cann-framework/vllm-ascend/blob/main/.github/workflows/lite-ci-dispatcher-smoke.yml)和[模型用例入口示例](tests/integration_tests/tools/lite_actions/entrypoint.py)。
- **Worker 运维者**在 [`config/workers.json`](config/workers.json) 中启用 Worker，在 [`config/pools.json`](config/pools.json) 中配置资源池归属、节点、设备组和容器绑定。用例可以指定 `worker`、`target_pool` 和 `docker`；调度器只在满足要求的完整分配上启动，不会为了等第二台机器先占住第一台。执行器使用已有容器，不负责创建容器或拉镜像。
- **GitCode 原生接入**不经过 GitHub 转发。按 `LITE_ACTIONS_GITCODE_REPOS` 配置仓库，准备对应的 `lite-ci-` 工作流、等待作业和回写身份。启用从指定提交读取 Python 用例的模式前，要在模型仓控制触发权限：来自 Fork 的用例代码可能以 Worker 的系统权限执行。

## 新部署：先启动业务 Worker

在 Linux 上使用 Python 3.11 或更高版本，准备 Git、Bash；远程执行 NPU 用例还需要 OpenSSH 和已验证的执行节点。在新机器或开发机上，可以先做**不接单、不占卡**的配置检查：

```bash
git clone https://github.com/depeng1994/lite-actions.git
cd lite-actions
python3 unified_router.py --worker-id blue --validate
```

预期输出包含 `Validated worker blue repositories:`。`blue` 是示例逻辑身份；部署 Yellow 时改为 `--worker-id yellow`。这一步不验证平台凭据、SSH 连通性或设备可用性。

正式接单前，给服务配置要监听的仓库（GitHub 使用 `LITE_ACTIONS_MODEL_REPOS`）、固定回写账号 `LITE_ACTIONS_RESULT_AUTHOR` 和受保护的凭据；GitCode 使用对应的 `LITE_ACTIONS_GITCODE_TOKEN`、`LITE_ACTIONS_GITCODE_RESULT_AUTHOR`。不要将 Token 写进仓库或传给训练节点。将 `LITE_ACTIONS_STATE_DIR` 指向跨版本不变的绝对路径，并沿用同一套资源锁。需要运行上面的本仓自检时，还须将 `depeng1994/lite-actions` 纳入监听列表、设置 `LITE_ACTIONS_SELF_SMOKE_ENABLED=1`，并让仓库变量 `LITE_CI_RESULT_ACTOR` 与回写账号一致。

用 [systemd 服务模板](deploy/lite-actions-blue.service)配置本机的 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`；其中 `unified_router.py --worker-id …` 才是实际接单的业务进程。已有服务不要再手工启动第二个 Worker。接入 NPU 前，先用 [`scripts/preflight.py`](scripts/preflight.py) 核验设备和容器；确认设备占用策略，`wait` 与允许抢占的 `preempt` 不能互换。

## 给已有 Worker 安装自动更新

**不同主机可以使用完全相同的服务名。** 例如，Blue 和 Yellow 的业务服务都可以叫 `lite-actions-native-pilot.service`。`--worker-unit` 指定“更新时停止并启动本机的哪个服务”；`--worker-id` 才指定“这个进程以哪个身份接单”。可以复用名称，不要因此把另一台机器的启动参数和资源配置也照搬过来。

下面是 **Blue 生产布局**的首装示例。已有自动更新的 Blue 不需要重装。另一台机器若同名、同布局，可以原样使用；若只是同名，只改 `ROOT`。`ROOT` 是含 `current` 和 `releases/` 的父目录，不是当前源码目录。

```bash
UNIT=lite-actions-native-pilot.service
ROOT=/opt/lite-actions/native-production
systemctl show "$UNIT" -p Id -p LoadState -p ActiveState -p WorkingDirectory
readlink -e "$ROOT/current"

# 在已审核、干净且与目标分支最新提交一致的 Lite Actions 工作树中执行
sudo bash scripts/install_auto_update.sh --worker-unit "$UNIT" --install-root "$ROOT" --dry-run
```

确认 `LoadState=loaded`、工作目录解析到同一份 `releases/<完整 SHA>`，且预检输出 `DRY_RUN_OK` 后，在允许中断任务的窗口去掉 `--dry-run` 进行安装。**这个脚本安装更新器，不创建业务 Worker，也不自动迁移旧目录。** 不要把 `lite-actions-auto-update.service` 当成 `--worker-unit` 传入。

```bash
sudo bash scripts/install_auto_update.sh --worker-unit "$UNIT" --install-root "$ROOT"
# 首装默认不开定时器；业务探测通过后启用
sudo systemctl enable --now lite-actions-auto-update.timer
systemctl list-timers --all lite-actions-auto-update.timer
journalctl -u lite-actions-auto-update.service -n 30 --no-pager
```

定时器在**上一轮结束约 3 分钟后**检查远端分支的 SHA，不是接收合入 webhook。SHA 不变不重启；发现可快进的新版本则准备独立发布目录、正常停止旧 Worker、原子切换 `current`，并在**同一轮**启动新 Worker、核验 Ready。新版本启动或就绪失败会尝试回滚代码，不回退业务状态。更新不会等待在途任务自然结束。要暂停以后各轮检查，执行 `sudo systemctl disable --now lite-actions-auto-update.timer`；这不会中止已经开始的升级。

## 深入阅读

| 我想做什么 | 入口 |
|---|---|
| 配置更新源、理解安装前提或处理回滚失败 | [自动更新运维指南](docs/auto-update.md) · [RFC #19：Blue 生产验收与适用边界](https://github.com/depeng1994/lite-actions/issues/19) |
| 开发用例、运行本地 CPU 回归 | [测试范围与命令](docs/testing.md) · [冒烟用例定义示例](tests/integration_tests/training_cases/dispatch_smoke.py) |
| 接入设备、排查取消或隔离 | [Blue 节点与运维背景](docs/blue-worker.md) · [隔离核验工具](scripts/reconcile.py) · [设备验证记录](docs/validation.md) |

平台只接收有大小限制的结果摘要；完整执行日志留在执行环境。排障时先按 **仓库、Run ID、重试次数和用例** 对齐日志。结果待回写不等于需要重跑训练；清理未经证实，也不要靠删除状态文件来解除隔离。
