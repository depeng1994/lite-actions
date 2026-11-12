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

### Yellow：GitCode / GitHub 仓库来源配置

Yellow 的模型仓来源按平台分别配置，不能把同一个仓库同时配置到两个平台：

- **TorchTitan NPU**：从 GitCode 仓库 `cann/torchtitan-npu` 发现 Workflow；配置 `LITE_ACTIONS_GITCODE_REPOS`。不要配置 GitHub 的 `torchtitan-npu`。
- **vLLM Ascend**：继续从 GitHub 仓库 `cann-framework/vllm-ascend` 发现 Workflow；配置 `LITE_ACTIONS_MODEL_REPOS`。

在 Yellow 已由 systemd `EnvironmentFile` 加载的 root-only 环境文件（例如 `/etc/lite-actions/yellow.env`）中维护以下配置。先确认现有文件，不要覆盖其中已有的 Worker、路径、Token 或其他环境设置：

```bash
sudo stat -c '%U:%G %a %n' /etc/lite-actions/yellow.env
sudoedit /etc/lite-actions/yellow.env
sudo chown root:root /etc/lite-actions/yellow.env
sudo chmod 0600 /etc/lite-actions/yellow.env
```

如果文件不存在，先按现有 Worker 的 systemd `EnvironmentFile` 配置确认正确路径，再创建新文件；不要用 `install /dev/null` 覆盖已有环境文件。

示例内容（占位符必须替换为已授权的账号和 Token；不要把真实凭据提交到仓库）：

```ini
# GitHub source: vLLM Ascend only
LITE_ACTIONS_MODEL_REPOS=cann-framework/vllm-ascend

# Router requires the common result-author identity.
LITE_ACTIONS_RESULT_AUTHOR=<github-result-account>

# GitCode source: TorchTitan NPU only
LITE_ACTIONS_GITCODE_REPOS=cann/torchtitan-npu
LITE_ACTIONS_GITCODE_TOKEN=<gitcode-personal-access-token>
LITE_ACTIONS_GITCODE_RESULT_AUTHOR=<gitcode-result-account>
```

`LITE_ACTIONS_GITCODE_RESULT_AUTHOR` 必须是对应 GitCode Token 所属、且有权限发布结果的账号。修改环境文件后，在确认没有不允许中断的在途任务后，按主机维护流程重启 Worker，使 systemd 重新加载环境。

**不要配置 GitHub 的 `torchtitan-npu`**：TorchTitan 的 Yellow 任务来源是 GitCode `cann/torchtitan-npu`；GitHub 侧保留 `cann-framework/vllm-ascend`。此配置不改变 GitCode/GitHub 的仓库 canonical alias，也不跟随 GitHub 301 重定向。

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
A push to the protected main branch runs the same checks in GitHub Actions. See [test scope](docs/testing.md)
before adding a scenario; configuration snapshots are not the test objective.

For trusted candidates, `.github/workflows/lite_actions_self_smoke.yml` can be
started manually after the Worker is running from a clean checkout of that exact
commit. It uses the repository's `tests/integration_tests/training_cases/dispatch_smoke.py`
full profile, in plan order: Blue OS, Yellow OS, single-node 8P, two-node 16P,
and a second single-node 8P. Both OS probes use zero NPUs. The independent
`cpu-only` deployment profile still probes Blue alone. The full suite runs
automatically after a PR lands on `main`, or manually on a deployed revision.
Edit `SMOKE_PROFILES` in the case definition above to change either suite.
`prepare` generates the GitHub matrix from the resolved plan, preserving order and
repeated instances; the workflow and Worker do not keep separate case lists.
Each planned instance has its own GitHub job. In a full run, `case_001_worker_os_yellow`
waits for the Yellow Worker, not the hosted runner: four passing Blue jobs cannot
satisfy it. A waiting job means its authenticated result is missing, not that the
Worker is necessarily offline. Compare the run SHA to Yellow’s `worker-ready.release_sha`
and check the effective GitHub discovery settings below. As in vLLM-Ascend Dispatcher Smoke, the final `Lite-Actions-Smoke` job requires
`prepare` and every selected matrix case to succeed.
For a fresh attempt, re-run **all** jobs so `prepare` publishes that attempt’s plan.

In each OS probe's matrix job, expand the Worker receipt step to see the executing
host, architecture, kernel, OS, Python, online logical CPUs, host-wide CPU busy
percentage over one second (excludes idle, I/O wait and steal), and 1/5/15-minute
load averages (not percentages). These are measurements from the assigned Worker,
not the GitHub runner. Older receipts can show only the fields they actually contain.

With GitHub polling enabled, set `LITE_ACTIONS_SELF_SMOKE_ENABLED=1` to opt in.
Leave the model repository list unchanged: if it omits `depeng1994/lite-actions`,
only the `lite_actions_self_smoke.yml` workflow is added to discovery (one GET
per minute when idle, no heartbeat writes). An explicit Lite Actions repository
subscription is not duplicated; disabling GitHub polling still disables this route.
Run `python3 unified_router.py --worker-id yellow --validate` in the service's
environment to inspect the discovery settings without sending a request. Set the
repository variable `LITE_CI_RESULT_ACTOR` to the Worker's result-author account.
A user with repository write, maintain or admin permission can start the workflow
from a repository branch whose current commit is deployed on the Worker:

```bash
gh workflow run lite_actions_self_smoke.yml -R depeng1994/lite-actions --ref <repository-branch>
```

The Worker rejects a run unless its SHA matches its own verified checkout. Keep
the configured resource locks and cleanup policy, and record the Run ID, SHA,
all five case results and cleanup evidence. Missing results or unverified cleanup
are failures. Record resource and timing evidence when evaluating FIFO; admission
order alone does not prove actual start order.

Stop the candidate gracefully after the run. Check for unfinished or quarantined
allocations and reconcile them as needed before restoring the previous Worker with
the same state and locks. Preserve logs and the evidence linking both SHAs, the Run
and both live Workers. The unit tests alone do not certify 8P/16P hardware execution.


## 安装自动更新

### 1. 运行只读自检

在经过审核的干净工作树中运行；需要 Python 3.11+、已有的 systemd Worker 服务，以及 `current → releases/<完整 SHA>` 的 Git checkout。以下以 Yellow 为例，显式指定解释器可避免 `sudo` 选中旧版 Python。

```bash
py=/usr/local/bin/python3.12
args=(--python "$py" --worker-unit lite-actions-yellow.service --install-root /opt/lite-actions)
# 仅代理主机：取消下一行注释，自检和安装将使用同一份文件。
# args+=(--proxy-env-file /etc/lite-actions/auto-update-proxy.source.env)
sudo "$py" scripts/auto_update_preflight.py "${args[@]}"
```

如果工作树属于普通部署用户、安装器通过 `sudo` 运行，Preflight 和安装器检查 clean 状态时使用 `git --no-optional-locks status`。它禁止 Git 为可选的 index 刷新获取/写入锁，避免 root 检查时把普通用户工作树的 `.git/index` 改成 root 所有；不会改变 status 的脏文件判断。无需为了安装把整个源码工作树 `chown` 给 root。

代理主机先创建专用文件：`sudo install -m 0600 /dev/null /etc/lite-actions/auto-update-proxy.source.env`，再用 `sudoedit` 填入代理/CA 配置。文件必须 root 所有、权限 `0600`、普通文件且非软链；仅接受 `http_proxy`、`https_proxy`、`HTTP_PROXY`、`HTTPS_PROXY`、`no_proxy`、`NO_PROXY`、`SSL_CERT_FILE`、`CURL_CA_BUNDLE`、`GIT_SSL_CAINFO` 九个 `KEY=VALUE`，不要传含 Token 的 `yellow.env`。无代理主机不用建文件。自检出现 `FAIL` 时按 `Next:` 提示处理；没有 `.git` 的快照须先迁移。

### 2. 安装并启用

自检通过后，在同一个 Bash 会话中执行安装。安装可能中断在途任务，默认不开启定时更新。

```bash
sudo bash scripts/install_auto_update.sh "${args[@]}"
```

业务验证通过后，执行 `sudo systemctl enable --now lite-actions-auto-update.timer`。日志与停用命令见[自动更新运维指南](docs/auto-update.md)。
