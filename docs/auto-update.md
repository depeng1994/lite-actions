# Automatic Worker Updates — 运维与恢复

设计依据：RFC [#19](https://github.com/depeng1994/lite-actions/issues/19)。自动更新器不识别部署区域，仅通过现有 systemd Worker Unit 名称定位目标。禁止将它用作跨网络 Push 或清理设备隔离的工具。

## 工作原理

- systemd Timer 在上一轮 oneshot 更新结束后约 3 分钟启动下一轮 Service。
- Service 调用固定的 /opt/lite-actions/bootstrap/update.sh；Bootstrap 固定本轮源码目录，执行 Python 模块 auto_update.update。
- check_source.py 按完整 SHA 检测和检出，version_switch.py 通过 systemd 正常停止旧 Worker、原子替换 current、立即启动新进程并检查 Ready。
- 事务保存在 /var/lib/lite-actions/auto-update，管理日志在 /var/log/lite-actions/management/updates.jsonl；Actions 日志及 NPU 状态独立保留。
- 不做忙时跳过，不做调度暂停、排空和恢复；升级可能导致正在运行的 Actions 被取消。

## 首次部署前提

1. 获取已经审核的 Lite Actions Git 工作树；本地 HEAD 与设置的远端 ref HEAD 必须一致。
2. 机器必须运行真实 systemd，存在受管理的 Worker Unit；代码位于 /opt/lite-actions/releases/<40位SHA>，current 是指向它的软链接。否则先独立迁移，禁止强行安装。
3. 只读核对现有 Worker Unit、EnvFile、WorkingDirectory、StopTimeout、持久状态目录、资源锁和隔离；记录旧 SHA，不输出 Token。
4. current 的 config/state/logs/cache 若为外部软链接，只能指向现有受控目录。安装器不能覆盖业务状态和资源隔离。
5. 测试分支要使用明确的 --git-ref refs/heads/<branch> 参数，不能悄悄变成生产持续更新来源。
6. 首次安装可能正常停止/重启 Worker，打断在途任务。只有在允许中断任务的窗口进行。

## 一键首次安装

在已审核、干净的 Git 工作树：

~~~bash
sudo bash scripts/install_auto_update.sh --worker-unit EXISTING_WORKER.service

# 仅核对，不写入或重启
bash scripts/install_auto_update.sh --worker-unit EXISTING_WORKER.service --dry-run
~~~

安装器完成本地可信 Seed、Bootstrap、Timer/Service、root-only 环境文件、Ready drop-in、systemd verify、daemon-reload、首次检查和受控升级；同配置重入要幂等。默认不启用定时更新；E2E 合格后再执行：

~~~bash
sudo systemctl enable --now lite-actions-auto-update.timer
~~~

高级选项：--git-url、--git-ref、--install-root、--dry-run 和 --enable-timer。不得使用未审核的 curl | bash。

## Yellow GitCode 与 sudo 工作树注意事项

Yellow 的 TorchTitan Workflow 来源是 GitCode `cann/torchtitan-npu`；GitHub
只配置 `cann-framework/vllm-ascend`。不要在 Yellow 配置 GitHub
`cann-framework/torchtitan-npu`，不要修改 GitHub canonical alias，也不要自动
跟随 GitHub 301 到 GitCode。来源平台由 Worker 配置明确选择。

将以下占位配置写入 Yellow 已有的受保护 Worker EnvironmentFile（例如
`/etc/lite-actions/yellow.env`），而不是仓库文件：

```bash
sudo install -m 0600 /dev/null /etc/lite-actions/yellow.env
sudoedit /etc/lite-actions/yellow.env
```

示例内容（全部是假值/占位符）：

```ini
LITE_ACTIONS_MODEL_REPOS=cann-framework/vllm-ascend
LITE_ACTIONS_GITCODE_REPOS=cann/torchtitan-npu
LITE_ACTIONS_GITCODE_TOKEN=<gitcode-personal-access-token>
LITE_ACTIONS_GITCODE_RESULT_AUTHOR=depeng1994
LITE_ACTIONS_GITCODE_PROXY=http://<legacy-api-proxy-host>:<port>
```

EnvironmentFile 必须保持 root 所有、权限 `0600`，不要输出或提交文件内容。
`LITE_ACTIONS_GITCODE_PROXY` 只覆盖 `api.gitcode.com` 的 GitCode API opener；
未设置时沿用 urllib 默认的环境代理。它不改变 Git subprocess 的代理：
`git fetch` 仍使用通用 `https_proxy`/`HTTPS_PROXY` 和 CA 配置。无须独立
API 代理的主机不要设置此变量。TLS 校验不关闭。

从普通用户拥有的工作树以 sudo 执行安装器或 Preflight 时，Git `status`
调用会设置 `GIT_OPTIONAL_LOCKS=0`，禁止 Git 的可选 index refresh 把
`.git/index` 改写为 root 所有。该设置只关闭可选锁/刷新，不跳过工作树
dirty 检查，也不改变 Git 源或安装事务。

## 启动时延指标

| 指标 | 待实测验收目标 |
| --- | --- |
| 上次检查退出至下次检查 | 约 3 分钟，另有 10 秒合并唤醒窗口 |
| current 切换至 start 请求 | ≤5 秒 |
| current 切换至新 Worker PID | ≤30 秒 |
| start 请求至有效 Ready | ≤120 秒 |

这些是设计目标，不是生产实测。Git 下载和旧 Worker 清理时间另计。真实 Worker 模板最多可有 900 秒停止宽限，不得把 Ready 时限说成总升级耗时。

## 查看和停用

~~~bash
systemctl list-timers lite-actions-auto-update.timer
journalctl -u lite-actions-auto-update.service -n 100 --no-pager
sudo tail -n 50 /var/log/lite-actions/management/updates.jsonl
readlink -f /opt/lite-actions/current
git -C /opt/lite-actions/current rev-parse HEAD

# 仅停止未来检查，不中断正在进行的更新事务
sudo systemctl disable --now lite-actions-auto-update.timer

# 只恢复中断事务；不下载新代码
sudo /opt/lite-actions/bootstrap/update.sh --recover

# 只检查 SHA；不停止 Worker
sudo /opt/lite-actions/bootstrap/update.sh --check
~~~

结果包括 NO_CHANGE、UPDATED、FAILED_PREPARE、ROLLED_BACK 和 RECOVERY_REQUIRED。最后一种必须保留 transaction.json、SHA/current、MainPID、管理日志以及资源与隔离证据，人工判断恢复；不得清除未知设备隔离或杀外部进程。回滚只切换代码，不回滚业务状态。

## CPU-only E2E

本期验证范围遵循用户的 CPU-only 决定：Dispatch 验证配置暂时注释 8P/16P 用例，只执行 Worker OS Probe。**不能永久删除正式 Dispatch 的 NPU 能力。** 保存真实 Run ID、Case/回写结果、Worker SHA、管理日志和清理记录。未运行的 NPU 用例必须记为 NOT_TESTED。

如果选定远端是没有 systemd 的容器，只能开展 CPU DT，不得把模拟 systemctl 称作真实 E2E。生产部署失败注入要放到隔离环境，不通过向 main 故意合入坏代码实现。

## 安全边界

- 仅 Pull 可信 GitHub/GitCode 源，不自动混合镜像或跨来源切换。
- 候选源码固定完整 SHA、正常祖先关系，拒绝回退/分叉或不可信路径。
- 不读取/输出凭据，不新增日志外传通道，不修改既有 Pool/Router/Lease/隔离协议。
- 正式开启 Timer 前须确认分支审核、CI required checks 和发布治理。
