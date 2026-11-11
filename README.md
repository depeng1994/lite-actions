# Lite Actions

GitHub Actions is the control console. Independent Pull workers run the same router,
scheduler and executor to execute model-owned tests on leased NPU resources.
Training nodes never receive GitHub credentials. Full logs stay inside the execution
environment; each case publishes at most 20 diagnostic lines.

Successful training returns LOSS lines (rank 0 preferred); failed execution returns
the relevant error and traceback context. OS and communication smoke cases return
their own diagnostics. A training case without LOSS evidence is not reported PASS.

## Deployment

| Worker | Login | Pools | Validation scope |
| --- | --- | --- | --- |
| blue | `ssh ycy` | `a3_shared_blue` | OS, 8P/16P smoke, DSV4 8P Muon/AdamW |
| yellow | `ssh hwy` | `a3_shared_yellow`, `a5_shared_yellow` | OS probe; training pools disabled |

The current A3 nodes belong exclusively to blue:
`113.46.17.205:153` and `113.46.17.205:154`.
These are canonical SSH aliases. Deployment SSH configuration maps each alias to the
verified host connection, port, key and relay. Container connections are separate
from host PID / notification connections.

Blue production service paths, supported model entry and operations are documented in
[blue worker operations](docs/blue-worker.md).

## End-to-end flow

```text
Model repo: Nightly / Dispatcher Smoke (manual + PR)
  -> prepare at exact source SHA
  -> Workflow defaults merged with Python ci_overrides
  -> schema-3 immutable request Artifact + per-case Matrix Waiters
  -> blue / yellow independently Pull their own cases
  -> SAME schedule/execute implementation, one instance per worker:
       per-kind pool queue -> complete device/host/port lease -> stage pinned source
       -> existing pinned container -> model entrypoint -> verify -> clean
  -> identity-bound Commit Comment -> matching Waiter -> case result
```

No central push service or cross-region hardware lease is required: every physical
node has one worker owner. Inventory validation rejects duplicate nodes/fingerprints.
The model repository owns training arguments, CANN/assets and test verification.
This repository owns topology, physical device IDs, container bindings and SSH.
Optional per-node `capabilities` restricts admission to `train` and/or `npu_smoke`.
Omitting it preserves both capabilities; an empty list disables admission on that
node. All nodes in a multi-node allocation must support the case kind before any
lease is taken. The current `:154` deployment is smoke-only until its training
assets are configured; DSV4 cases queue on the asset-ready `:153` node.

## GitHub workflow and model cases

For each repository explicitly listed in `LITE_ACTIONS_MODEL_REPOS`, the Worker
discovers GitHub workflows whose name starts with `lite-ci-`, regardless of the
event that started them. The hosting platform controls who can trigger workflows.
The workflow's trusted preparation job publishes run- and attempt-bound
`lite-ci-request-<attempt>` and `lite-ci-source-<attempt>` artifacts. The latter
binds the selected commit or verified PR merge source to the controller, plan and
repository identity; the Worker independently verifies these before admission.
The workflow waits for all requested results. A prefix alone does not supply the
plan, source proof, model entrypoint, verified topology or execution environment.
The repository-owned self-smoke has its own pinned-Worker contract.

New repositories use the same model-owned entrypoint at
`tests/integration_tests/tools/lite_actions/entrypoint.py` and define their own
cases. Resource pools, allocation groups and container bindings are configured
by the Worker operator. A two-device case, for example, needs a verified group
of exactly two devices and a compatible bound container.

Python `OverrideDefinitions` adds `ci_kind` (`train`, `npu_smoke`, `worker_probe`)
and optional `ci_overrides` for `worker`, `target_pool`, `docker`. Explicit fields
override Workflow defaults; omitted fields inherit. Empty/unknown fields fail.
Worker probes inherit only worker identity and request no NPU/container resources.
`nnodes * ngpu` supplies displayed device count; verified topology controls allocation.

## Start and validation

Python 3.11+ on Linux is recommended. Required tools are Git, OpenSSH, and Bash.
The worker need not have Docker or NPU hardware; host/container operations run over SSH.
Use a fixed result-author identity and repository-authorized credentials:

```bash
export LITE_ACTIONS_RESULT_AUTHOR=<configured-result-login>
export LITE_ACTIONS_ALLOWED_ACTORS=<comma-separated-authorized-logins>
export LITE_ACTIONS_ALLOWED_BRANCHES=master
export LITE_ACTIONS_STATE_DIR=/var/lib/lite-actions/blue
# Set LITE_ACTIONS_GITHUB_TOKEN securely, or authenticate gh on this worker.
python3 unified_router.py --worker-id blue --validate
python3 -m unittest discover -s tests
./run-unified-router.sh --worker-id blue
```

Use `--worker-id yellow` on hwy. Run in the deployment supervisor or redirect stdout
to a protected system log. The process uses a worker-host single-instance lock;
resource leases are shared across checkouts of the same deployment user.

Keep `LITE_ACTIONS_STATE_DIR` at one absolute, worker-specific location across
releases. The worker and management commands must use the same value. State updates
take a process lock before reading and writing, so reconciliation cannot overwrite
another task's progress or pending result. Without this setting, state stays under
the checkout's `state/` directory for local development.

## Existing-container onboarding

`docker` names an existing container or a shared logical environment. Trusted
per-node `environments` entries pin its full container ID, image ID and image reference. The executor does not create containers,
pull images, or choose among ambiguous candidates. It checks the running container
and current image tag before NPU admission. NPU execution requires root-level PID
visibility in the execution namespace so cleanup can verify every marked descendant. Different jobs use isolated directories.

```bash
python3 scripts/preflight.py --worker-id blue
# After explicit container bindings are selected and all checks pass:
python3 scripts/preflight.py --worker-id blue --write-bindings
```

Batch SSH explicitly disables PTY allocation. Source synchronization uses SSH stdin streaming, avoiding SCP's ambiguity for aliases
containing a colon. The archive checksum is verified remotely. All nodes must stage
successfully before any rank launches. Test outputs remain below a per-run/attempt/
case directory on the approved mount.

## Scheduling, cancellation and recovery

Within each pool on one worker, Smoke cases from GitCode and GitHub follow their
durable admission sequence. Legacy training cases retain run creation time, run ID
and declaration order. A resource-blocked queue head blocks only its own kind and
pool; neither kind has fixed priority. Both share physical device, host and port
leases and the four resource-execution slots. A separate local probe slot is
available. Independent complete allocations run in parallel; distributed requests
never retain partial leases.

Each node can set `occupancy_policy` to `wait` or `preempt`. Without an override,
the pool's existing `force_reclaim` setting applies. A `wait` node never kills
external processes; a busy group is temporarily avoided according to the queue
policy. Both configured Blue nodes use `wait` even though their pool defaults to
`preempt`.

New `preempt` policies use confirmed host PIDs on the selected devices before launch;
its cleanup is read-only. It requires complete per-node `device_ids`, a matching
`machine_fingerprint` from `scripts/preflight.py`, and host support for pidfd.
Each host scan verifies its hardware inventory and Phy-ID mapping before signalling.
To register a new node without a fingerprint, temporarily set its policy to `wait`,
run `scripts/preflight.py --write-bindings`, then remove the override or set
`preempt` after checking the generated binding. Until then, configuration loading
rejects a node that inherits `preempt` without a fingerprint.
For a multi-node allocation, waiting nodes are checked before preemption.
Yellow A5 nodes without an explicit policy retain the pool's legacy
`force_reclaim` behavior, including A5 cleanup. Cancellation is checked before
each preemption request; unconfirmed cleanup is reported as such.

The runtime `force_reclaim` field is the pool default. `OCCUPANCY_POLICY` log
lines show the effective policy for each selected node and execution phase.

For GitCode repositories listed in `LITE_ACTIONS_GITCODE_REPOS`, the Worker also
discovers running workflows whose name starts with `lite-ci-`, regardless of
event. For the source-profile protocol, each participating worker resolves
`dispatch_smoke` from the run's fixed model
commit, accepts only its own cases, and publishes results as the configured
`LITE_ACTIONS_GITCODE_RESULT_AUTHOR` using that account's token. The waiter job must be uniquely named
`Dispatcher Smoke`. GitCode PR runs accept Fork sources; discovery executes the
source's Python directly on the worker, so this mode grants Fork code the worker's
operating-system permissions. A new dispatch of the same run gets separate task,
log and result identities.

Cancellation terminates only the case's marked ranks and checks NPU occupancy.
State is atomically persisted and fsynced before execution. Restart never blindly
reruns a case; pending reports retry without training. With the default policy,
unknown remote state quarantines the allocation until reconciliation:

```bash
python3 scripts/reconcile.py --worker-id blue --task-key '<persisted-task-key>'
```

The command checks owned processes and device occupancy before removing quarantine.
It never kills processes.

For an explicitly selected trial worker, `LITE_ACTIONS_DISABLE_QUARANTINE_WORKERS=blue`
disables automatic quarantine for that worker. On startup its prior quarantine
flags are cleared while task and result records remain; other workers retain their
default policy. Device, host and port locks, occupancy checks and cancellation are
unchanged, and unverified cleanup is still reported as `cleanup_verified=false`.

## Result limits

The request and result bind repo, workflow, run, attempt, SHA, plan digest, case, worker, pool and
environment. Waiters also require the configured result author and reject PASS when
cleanup is unverified. Metadata-only progress updates contain no training output.

- At most **20 lines per case**, each at most **180 characters**.
- Comment body at most **6100 UTF-8 bytes**; serialized API request at most **7000 bytes**.
- Lines are removed if needed to satisfy both byte budgets after JSON serialization.
- No full logs, TensorBoard, extra traceback or training artifacts are uploaded via
  Summary, other comments or Artifacts. Summary contains metadata and status only.
- A partial Matrix rerun without its attempt's plan fails explicitly; rerun all jobs
  or start a new manual run with the desired test selectors.

## Migration and hardware gates

Drain/cancel old work, verify remote release, stop old dispatchers, archive state,
install canonical aliases and deploy the same reviewed release to both workers.
Enable each model workflow only after its matching Worker contract is deployed;
remove retired routes and update required status checks during the cutover.

Yellow training pools remain disabled until independent hardware, container mounts,
CANN, asset paths, network, device numbering and real smoke/training/cleanup checks
have been accepted. A5 registration alone does not certify 64P capability.

See [implementation validation](docs/validation.md) for actual evidence and remaining gates.

Before the first hardware switch, preview legacy quarantine mapping without changing
state or contacting devices:

```bash
python3 scripts/import_quarantine.py --worker-id blue --legacy-root /root/work/lite-actions
```

`--apply` is permitted only after every legacy task is terminal and the legacy
process is stopped. Mapping uses the trusted node IP inventory, rejects unknown
allocations, and preserves unscoped quarantine across the entire owned pool. Legacy
guards retain their original job keys for manual evidence-based recovery; the normal
reconciliation command applies to current tasks with their persisted runtime records.

To map an environment key to a differently named container, use a trusted
JSON file such as `{"113.46.17.205:153":"<existing-container-name-or-id>"}`:

```bash
python3 scripts/preflight.py --worker-id blue --node 113.46.17.205:153 \
  --bindings-file /protected/path/container-bindings.json --write-bindings
```

This is read-only with respect to execution hosts and does not enable a pool or
start training. The default selects the environment's exact container name; an
existing binding is always verified by its full container ID.
`docker` may also name a shared logical environment. With explicit container
selection, each node pins its container ID, image ID and actual `image_ref` under
that key. Runtime checks both the running container and the node's image reference;
changing either identity requires an explicit new binding. Identical environment
names do not certify identical dependencies or multi-node compatibility.
An optional node `pythonpath` can point to an operator-provisioned, pinned dependency
directory. It precedes inherited container overrides while preserving the staged
model source first. This allows CI to use the model repository's required upstream
checkout without replacing packages used by other tasks in the shared container.

## Development checks

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/lint.py
python3 -m unittest discover -s tests
```

The lint command runs Ruff lint/format checks and requires `device_ids`/`devices`
arrays in `config/pools.json` to remain on one line. Use `--fix` to format both.
Pull requests run the same checks in GitHub Actions. See [test scope](docs/testing.md)
before adding a scenario; configuration snapshots are not the test objective.

For trusted candidates, `.github/workflows/lite_actions_self_smoke.yml` can be
started manually after the Worker is running from a clean checkout of that exact
commit. It uses the repository's `tests/integration_tests/training_cases/dispatch_smoke.py`
profile: a Blue OS probe followed by 8P, 16P and 8P HCCL Smoke. The Yellow OS
probe is disabled in the default profile. PRs continue to run CPU checks only.

To opt a Worker into the self-smoke route, include `depeng1994/lite-actions` in
`LITE_ACTIONS_MODEL_REPOS` and set `LITE_ACTIONS_SELF_SMOKE_ENABLED=1`. Set the
repository variable `LITE_CI_RESULT_ACTOR` to the Worker's result-author account.
A user with repository write, maintain or admin permission can start the workflow
from a repository branch whose current commit is deployed on the Worker:

```bash
gh workflow run lite_actions_self_smoke.yml -R depeng1994/lite-actions --ref <repository-branch>
```

The Worker rejects a run unless its SHA matches its own verified checkout. Keep
the configured resource locks and cleanup policy, and record the Run ID, SHA,
all four case results and cleanup evidence. Missing results or unverified cleanup
are failures. Record resource and timing evidence when evaluating FIFO; admission
order alone does not prove actual start order.

Stop the candidate gracefully after the run. Check for unfinished or quarantined
allocations and reconcile them as needed before restoring the previous Worker with
the same state and locks. Preserve logs and the evidence linking both SHAs, the Run
and the live Worker. The pull-request checks above run CPU contracts only; they do
not certify 8P/16P hardware execution.


## 自动更新部署（首次安装）

Lite Actions 的 Worker 通过 systemd Timer 主动检查可信 Git 源。默认在上一轮检查结束约 3 分钟后再次检查；发现新版本后正常停止旧 Worker、切换 Release，并立即启动新 Worker。更新管理日志与 Actions 业务日志分开保存。

### 1. 部署前检查

自动更新器要求 **Python 3.11 或更高版本**。Ubuntu 20.04 的 /usr/bin/python3 可能仍是 Python 3.8，不能用于本功能。安装器默认查找 python3，也可以显式传入解释器绝对路径。sudo 通常会重置 PATH，所以不要依赖用户 Shell 中的 Python 搜索路径。

先确认本机现有 Worker 的 systemd 服务名：

~~~bash
systemctl list-units --all --type=service | grep -E 'lite-actions.*\.service'
systemctl status lite-actions-yellow.service --no-pager
~~~

--worker-unit 传入目标机器上**实际管理 Worker 进程**的 .service 名称。例如 Yellow：

~~~text
lite-actions-yellow.service
~~~

不同机器可以使用相同服务名；更新器只操作本机配置的服务，不判断 Blue/Yellow。不要传 lite-actions-auto-update.service，它是更新器自己的服务。

首次部署还要求现有目录为 Git Release 布局：

- /opt/lite-actions/current 是软链接；
- 目标目录为 /opt/lite-actions/releases/<完整40位SHA>；
- Release 必须是可验证的 Git checkout，包含 .git 元数据；
- config/、logs/、cache/ 必须存在且绑定有效。

如果当前版本是 git archive、rsync 或其他无 .git 元数据的快照，安装器会拒绝部署。**先单独完成 Release 布局迁移，不要强行安装或覆盖业务目录。**

### 2. 运行只读 Preflight

使用经过审核、干净的 Lite Actions Git 工作树。以下示例使用 Python 3.12：

~~~bash
sudo /usr/local/bin/python3.12 scripts/auto_update_preflight.py \
  --python /usr/local/bin/python3.12 \
  --worker-unit lite-actions-yellow.service \
  --install-root /opt/lite-actions \
  --git-url https://github.com/depeng1994/lite-actions.git \
  --git-ref refs/heads/main
~~~

Preflight 会逐项检查 Python、Git、systemd、Worker Unit、current/releases Git 布局、目录绑定、源工作树 clean/HEAD，以及 GitHub HTTPS 源可达性。失败项会显示下一步处理命令；Preflight 只读，不修改服务、文件或任务状态。

代理主机在 Preflight 命令末尾追加 `--proxy-env-file /etc/lite-actions/auto-update-proxy.source.env`，让只读检查也使用同一代理与 CA 配置；无代理主机不传此参数。

### 3. 首次安装

无代理的主机不需要额外参数：

~~~bash
sudo bash scripts/install_auto_update.sh \
  --worker-unit lite-actions-yellow.service \
  --install-root /opt/lite-actions \
  --python /usr/local/bin/python3.12
~~~

安装器会再次执行完整 Preflight，然后安装 Bootstrap、systemd Timer/Service 和解释器配置，执行首次检查与 Ready 验证。--dry-run 也会先执行完整 Preflight，但不会写入配置或重启 Worker：

~~~bash
sudo bash scripts/install_auto_update.sh \
  --worker-unit lite-actions-yellow.service \
  --install-root /opt/lite-actions \
  --python /usr/local/bin/python3.12 \
  --dry-run
~~~

### 4. 需要 HTTPS 代理的主机

如果 Worker 必须通过内网 HTTPS 代理访问 GitHub，准备一个**专供 Auto Update 使用的代理文件**。该文件只能包含下列九种 KEY=VALUE，不能直接传入包含其他 Worker 配置或 Token 的完整 yellow.env：

~~~bash
sudo install -m 0600 /dev/null /etc/lite-actions/auto-update-proxy.source.env
sudoedit /etc/lite-actions/auto-update-proxy.source.env
~~~

允许的键名：

~~~text
http_proxy
https_proxy
HTTP_PROXY
HTTPS_PROXY
no_proxy
NO_PROXY
SSL_CERT_FILE
CURL_CA_BUNDLE
GIT_SSL_CAINFO
~~~

文件必须是 root 所有的普通文件、不能是软链接，权限必须为 0600。代理地址、认证信息和 CA 路径只保存在主机文件中，不写入仓库或命令参数；不要在终端、Issue 或日志中输出这些值。

安装时显式提供该文件：

~~~bash
sudo bash scripts/install_auto_update.sh \
  --worker-unit lite-actions-yellow.service \
  --install-root /opt/lite-actions \
  --python /usr/local/bin/python3.12 \
  --proxy-env-file /etc/lite-actions/auto-update-proxy.source.env
~~~

安装器会创建权限为 0600 的托管副本 /etc/lite-actions/auto-update-proxy.env，并添加 systemd drop-in。相同内容重复安装是幂等的；若源文件内容变化或现有托管文件不一致，安装器会拒绝覆盖，要求先备份、审查并显式处理。无代理主机**不要传 --proxy-env-file**，不会生成代理文件或代理 drop-in。

### 5. 启用与日常管理

首次安装默认不启用周期更新。完成目标环境的业务验收后，再启用：

~~~bash
sudo systemctl enable --now lite-actions-auto-update.timer
~~~

查看检查计划与更新日志：

~~~bash
systemctl list-timers lite-actions-auto-update.timer
journalctl -u lite-actions-auto-update.service -n 50 --no-pager
~~~

暂停未来的定时检查：

~~~bash
sudo systemctl disable --now lite-actions-auto-update.timer
~~~

切换版本后会在同一次更新事务中立即请求启动新 Worker，不等待下一轮 3 分钟检查。性能验收目标为切换后 5 秒内发出启动请求、30 秒内观察到新 PID、120 秒内达到 Ready；Git 下载和旧 Worker 任务清理时间另计。升级可能取消正在执行的 Actions。详细恢复与排障说明见 [自动更新运维文档](docs/auto-update.md)，设计与验证记录见 [RFC #19](https://github.com/depeng1994/lite-actions/issues/19)。
