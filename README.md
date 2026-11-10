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

## Workflow defaults and Python overrides

The model repository exposes only `nightly_fulltests.yml` and
`lite_actions_dispatcher_smoke.yml` as Lite Actions business workflows. Both support
`workflow_dispatch` and PRs against `master`; no scheduled run is added.

```yaml
default_worker: blue
default_target_pool: a3_shared_blue
default_docker: cann_master_torchtitan_master_xyj
default_cases: '[{"suite":"a3_8p_tests"}]'
```

Python `OverrideDefinitions` adds `ci_kind` (`train`, `npu_smoke`, `worker_probe`)
and optional `ci_overrides` for `worker`, `target_pool`, `docker`. Explicit fields
override Workflow defaults; omitted fields inherit. Empty/unknown fields fail.
Worker probes inherit only worker identity and request no NPU/container resources.
`nnodes * ngpu` supplies displayed device count; verified topology controls allocation.

Nightly uses actual model definitions `dsv4_flash_a3_8p_example` and
`dsv4_flash_a3_8p_adamw`, each 1x8 and five steps. Their existing `check_loss=False`
means PASS does not imply a new numerical-accuracy certification.

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
policy. Both configured Blue nodes use `wait`.

Explicit `preempt` uses confirmed host PIDs on the selected devices before launch;
its cleanup is read-only. It requires complete per-node `device_ids`, a matching
`machine_fingerprint` from `scripts/preflight.py`, and host support for pidfd.
Each host scan verifies its hardware inventory and Phy-ID mapping before signalling.
For a multi-node allocation, waiting nodes are checked before preemption.
Nodes without an explicit policy retain the pool's legacy
`force_reclaim` behavior, including A5 cleanup. Cancellation is checked before
each preemption request; unconfirmed cleanup is reported as such.

The runtime `force_reclaim` field is the pool default. `OCCUPANCY_POLICY` log
lines show the effective policy for each selected node and execution phase.

For the GitCode source-profile pilot, list its workflow filenames in both
`LITE_ACTIONS_GITCODE_WORKFLOWS` and `LITE_ACTIONS_GITCODE_SOURCE_WORKFLOWS`.
Each participating worker resolves `dispatch_smoke` from the run's fixed model
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

Schema 3 binds repo, workflow, run, attempt, SHA, plan digest, case, worker, pool and
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
install canonical aliases and deploy the same schema-3 release to both workers.
Only then enable the two new workflows and update required status checks. Keep the
model repository's upstream/mirror synchronization consistent with these workflows.

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

For a trusted candidate that needs real-hardware verification, use a maintenance
window: drain and stop the existing Worker, then start `unified_router.py` from a
clean checkout of the reviewed Lite Actions SHA under the deployment supervisor.
Keep the same single-instance lock, shared device locks, configuration and durable
state, including quarantines. Confirm the live process and checkout actually match
the candidate SHA before manually triggering the model repository's default
8P/16P/8P Smoke at its reviewed SHA. Record the platform Run ID and verify the model
SHA, all three case identities, results and successful cleanup against that Run.
Missing or unknown results and unverified cleanup are failures. Record the resource
and timing evidence needed to assess contention and FIFO; admission order alone
does not prove the actual start order.

Stop the candidate gracefully after the run. Check for unfinished or quarantined
allocations and reconcile them as needed before restoring the previous Worker with
the same state and locks. Preserve logs and the evidence linking both SHAs, the Run
and the live Worker. The pull-request checks above run CPU contracts only; they do
not certify 8P/16P hardware execution.
