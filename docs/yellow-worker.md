# Yellow Worker 仓库来源配置

Yellow 的 Worker 同时发现 GitHub 与 GitCode 上的 Workflow。两个平台的仓库来源必须分别配置，不能把 TorchTitan 错配到 GitHub。

## 仓库归属

| 模型仓 | Worker 发现平台 | 配置 |
| --- | --- | --- |
| TorchTitan NPU | GitCode | `LITE_ACTIONS_GITCODE_REPOS=cann/torchtitan-npu` |
| vLLM Ascend | GitHub | `LITE_ACTIONS_MODEL_REPOS=cann-framework/vllm-ascend` |

**不要把 `torchtitan-npu` 配置到 GitHub。** Yellow 的 TorchTitan 任务从 GitCode `cann/torchtitan-npu` 发现；vLLM Ascend 仍从 GitHub `cann-framework/vllm-ascend` 发现。此配置不修改 GitHub canonical alias，也不自动跟随 301 重定向。

## Yellow 环境文件

将配置合并到 Yellow systemd Worker 已加载的 root-only EnvironmentFile（现场通常为 `/etc/lite-actions/yellow.env`）。先检查已有文件和权限，再编辑；**不要用空文件覆盖现有 Worker 环境配置**：

```bash
sudo stat -c '%U:%G %a %n' /etc/lite-actions/yellow.env
sudoedit /etc/lite-actions/yellow.env
sudo chown root:root /etc/lite-actions/yellow.env
sudo chmod 0600 /etc/lite-actions/yellow.env
```

配置示例，所有尖括号内容均为占位符：

```ini
# GitHub: vLLM Ascend only
LITE_ACTIONS_MODEL_REPOS=cann-framework/vllm-ascend
LITE_ACTIONS_RESULT_AUTHOR=<github-result-account>

# GitCode: TorchTitan NPU only
LITE_ACTIONS_GITCODE_REPOS=cann/torchtitan-npu
LITE_ACTIONS_GITCODE_TOKEN=<gitcode-personal-access-token>
LITE_ACTIONS_GITCODE_RESULT_AUTHOR=<gitcode-result-account>
```

- `LITE_ACTIONS_GITCODE_TOKEN` 必须是对 GitCode 目标仓库有读取 Workflow/PR、发布结果所需权限的 Token。
- `LITE_ACTIONS_GITCODE_RESULT_AUTHOR` 必须与该 Token 对应的结果发布账号一致，并有权对目标提交发布结果。
- `LITE_ACTIONS_RESULT_AUTHOR` 是 Router 的通用结果作者身份；GitCode 结果作者可由 `LITE_ACTIONS_GITCODE_RESULT_AUTHOR` 单独覆盖。
- 不要把真实 Token 写入仓库、命令行参数、Issue、PR 或日志。

编辑后核对权限为 root:root、0600。只有在确认没有不允许中断的在途任务后，才按 Yellow 的维护流程重启 Worker，使 systemd 重新加载 EnvironmentFile。

## 安装器对普通用户工作树的保护

Yellow 上的部署工作树可能归普通用户所有，而安装器以 `sudo` 运行。安装器和只读 Preflight 检查 clean 状态时使用：

```bash
git --no-optional-locks status --porcelain --untracked-files=all
```

`--no-optional-locks` 禁止 Git 为可选 index 刷新获取/写入锁，避免 root 执行只读状态检查时把用户工作树的 `.git/index` 改成 root 所有。它不改变脏文件判断，也不授予安装器修改源码的权限；不要因此把整个工作树递归 `chown` 给 root。
