# blue worker 生产运行

blue 在 `ssh ycy` 上由 `lite-actions-blue.service` 管理，已设置开机启用和故障重启。当前发布代码为 `2c5adbf934a65442e6aaffe4cf7a936ea9468265`，对应 Lite Actions main；服务配置模板见 [systemd unit](../deploy/lite-actions-blue.service)。

## 路径与进程

| 内容 | 生产路径 |
| --- | --- |
| 发布与当前版本 | `/opt/lite-actions/releases/<SHA>`、`/opt/lite-actions/current` |
| 常用访问入口 | `/root/work/lite-actions`，指向当前发布 |
| 节点/容器绑定配置 | `/etc/lite-actions/blue/` |
| 凭据和运行环境 | `/etc/lite-actions/blue.env`，仅 root 可读 |
| 任务、结果重试与隔离状态 | `/var/lib/lite-actions/blue/` |
| 执行器日志 | `/var/log/lite-actions/blue/` |
| 固定 SHA 源码缓存 | `/var/cache/lite-actions/blue/` |
| 发布信息 | `/etc/lite-actions/blue-deployment.json` |

代码发布目录中的 config/state/logs/cache 指向上述独立目录。`LITE_ACTIONS_STATE_DIR` 固定到生产状态目录，状态写入持有进程锁，升级与管理操作不会改用空白状态文件。资源锁沿用该主机的公共锁目录；服务同时持有旧全局进程锁，防止归档入口误启动。

旧生产目录已归档，旧调度进程已停止。原有 `113.46.17.205:153` 设备 0–7 的隔离记录保留。本次未解除该隔离，也不抢占外部任务。

## 当前调用范围

模型仓 PR #11 暂不合入：blue 使用模型仓现有 PR 分支的 schema 3 入口，尚未切换 master 与 required checks。允许的手动分支记录在发布信息的 `model_branch` 中。yellow 未正式上线，两个 yellow 训练池禁用。

在 worker 主机上读取当前模型分支并提交任务：

```bash
MODEL_CI_REF=$(python3 -c 'import json; print(json.load(open("/etc/lite-actions/blue-deployment.json"))["model_branch"])')
gh workflow run nightly_fulltests.yml -R torchtitan-npu/torchtitan-npu --ref "$MODEL_CI_REF"
gh workflow run lite_actions_dispatcher_smoke.yml -R torchtitan-npu/torchtitan-npu --ref "$MODEL_CI_REF" \
  -f 'test_cases=[{"test_id":"worker_os_blue"},{"test_id":"dispatch_smoke_8p"},{"test_id":"dispatch_smoke_16p"}]'
```

Nightly 默认两条 DSV4 8P 均由 blue 执行。Dispatcher-Smoke 的模型仓默认集合还包含 yellow 系统探测，当前使用上述 blue 选择器。模型仓同步策略仍待处理；本次 blue 上线不修改模型仓同步工作流，也不保证 GitHub 独有分支不会被未来同步删除。

两个节点都进入已有容器 `cann_master_torchtitan_master_xyj`，逐节点固定容器与镜像身份。`:153` 可运行训练与 smoke；`:154` 缺少已配置的 DSV4 资产，仅允许 smoke。现有资源条件下两条 DSV4 训练排队，独立可用的 smoke 分配可以并行。

## 运维

```bash
systemctl status lite-actions-blue.service
journalctl -u lite-actions-blue.service -f
systemctl is-enabled lite-actions-blue.service
```

停止或重启服务时，主进程先取消并清理本服务所属任务，再退出。systemd 的停止宽限为 900 秒；无法证明释放的 allocation 在持久状态中保留隔离，重启不盲目重跑。维护操作应先确认当前任务和状态，不能依靠杀掉主进程来释放远端 NPU。

管理命令使用同一状态目录：

```bash
export LITE_ACTIONS_STATE_DIR=/var/lib/lite-actions/blue
python3 /opt/lite-actions/current/scripts/reconcile.py --worker-id blue --task-key '<已核对的任务键>'
```

该命令只在进程和设备均已核验后解除对应隔离，不杀进程。迁移保留的旧隔离缺少当前协议的完整运行记录，需要原任务退出证据和人工核对，不能直接删除记录来解锁。

每个用例仍最多回传 20 行、每行最多 180 字符，Comment/请求预算为 6100/7000 字节。完整日志和 TensorBoard 留在执行环境。当前实机证据和仍待完成的验证见 [验证记录](validation.md)。
