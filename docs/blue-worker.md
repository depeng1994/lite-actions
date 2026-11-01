# blue worker 生产运行

blue 在 `ssh ycy` 上由 `lite-actions-blue.service` 管理，已设置开机启用和故障重启。当前发布 SHA、模型分支和验收记录以 `/etc/lite-actions/blue-deployment.json` 为准；服务配置模板见 [systemd unit](../deploy/lite-actions-blue.service)。

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

blue 使用 schema 3 入口；发布前可验证 PR 分支，合入后使用 master。允许的手动分支记录在发布信息的 `model_branch` 中，升级不得更改 required checks。Yellow/A5 不属于本次 Blue 验收范围，保持其现有独立配置。

在 worker 主机上读取当前模型分支并提交任务：

```bash
MODEL_CI_REF=$(python3 -c 'import json; print(json.load(open("/etc/lite-actions/blue-deployment.json"))["model_branch"])')
gh workflow run nightly_fulltests.yml -R cann-framework/torchtitan-npu --ref "$MODEL_CI_REF"
gh workflow run lite_actions_dispatcher_smoke.yml -R cann-framework/torchtitan-npu --ref "$MODEL_CI_REF" \
  -f 'test_cases=[{"test_id":"worker_os_blue"},{"test_id":"dispatch_smoke_8p"},{"test_id":"dispatch_smoke_16p"}]'
```

Nightly 默认执行 8P Muon、8P AdamW 和双节点 16P AdamW 五步训练，均由 blue 执行。Dispatcher-Smoke 默认集合为 Blue 系统探测及 8P/16P HCCL Smoke；Yellow 只能显式选择。模型仓同步策略仍待处理；本次 blue 上线不修改模型仓同步工作流，也不保证 GitHub 独有分支不会被未来同步删除。

两个节点都进入已有容器 `cann_master_torchtitan_master_xyj`，逐节点固定容器与镜像身份。16P 训练需要两端都具备用例所需资产与依赖；必须完成预检后才能在持久配置中启用第二节点的 `train` 能力，不能仅修改能力标记。训练与 Smoke 按完整设备及端口租约排队或并发，保留原有设备隔离，不抢占 Blue 外部任务。

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
