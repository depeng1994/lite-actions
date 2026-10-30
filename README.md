# Lite Actions

GitHub Actions is the control console. Independent Pull workers run the same router,
scheduler and executor to execute model-owned tests on leased NPU resources.
Training nodes never receive GitHub credentials. Full logs stay inside the execution
environment; each case publishes at most 20 diagnostic lines.

Successful training returns LOSS lines (rank 0 preferred); failed execution returns
the relevant error and traceback context. OS and communication smoke cases return
their own diagnostics. A training case without LOSS evidence is not reported PASS.

## Deployment

| Worker | Login | Pools | First-release acceptance |
| --- | --- | --- | --- |
| blue | `ssh ycy` | `a3_shared_blue` | OS, 8P/16P smoke, DSV4 8P Muon/AdamW |
| yellow | `ssh hwy` | `a3_shared_yellow`, `a5_shared_yellow` | OS probe; training pools disabled |

The current A3 nodes belong exclusively to blue:
`113.46.17.205:153` and `113.46.17.205:154`.
These are canonical SSH aliases. Deployment SSH configuration maps each alias to the
verified host connection, port, key and relay. Container connections are separate
from host PID / notification connections.

## End-to-end flow

```text
Model repo: Nightly / Dispatcher Smoke (manual + PR)
  -> prepare at exact source SHA
  -> Workflow defaults merged with Python ci_overrides
  -> schema-3 immutable request Artifact + per-case Matrix Waiters
  -> blue / yellow independently Pull their own cases
  -> SAME schedule/execute implementation, one instance per worker:
       pool FIFO -> complete device/host/port lease -> stage pinned source
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
# Set LITE_ACTIONS_GITHUB_TOKEN securely, or authenticate gh on this worker.
python3 unified_router.py --worker-id blue --validate
python3 -m unittest discover -s tests
./run-unified-router.sh --worker-id blue
```

Use `--worker-id yellow` on hwy. Run in the deployment supervisor or redirect stdout
to a protected system log. The process uses a worker-host single-instance lock;
resource leases are shared across checkouts of the same deployment user.

## Existing-container onboarding

`docker` is an image reference mapped by trusted per-node `environments` entries to
an existing full container ID and image ID. The executor does not create containers,
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

Within each pool, accepted cases follow run creation time, run ID and declaration
order. Independent complete allocations run in parallel. The first release does
not backfill past a blocked queue head. Four resource-execution slots and one local
probe slot are independent. Distributed requests never retain partial leases.

A busy external device group is temporarily avoided so another verified group can
be tried. Unknown occupancy is never killed. Scoped external reclaim remains subject
to explicit per-pool approval and PID identity verification.

Cancellation terminates only the case's marked ranks. Release requires NPU checks;
unknown remote state quarantines the allocation. State is atomically persisted and
fsynced before execution. Restart never blindly reruns a case; pending reports retry
without training. Unresolved allocations remain isolated until reconciliation:

```bash
python3 scripts/reconcile.py --worker-id blue --task-key '<persisted-task-key>'
```

The command checks owned processes and device occupancy before removing quarantine.
It never kills processes. Do not delete quarantine records to bypass these checks.

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
v3 reconciliation command applies to v3 tasks with their persisted runtime records.

For an explicit selection when multiple containers share an image, use a trusted
JSON file such as `{"113.46.17.205:153":"<existing-container-name-or-id>"}`:

```bash
python3 scripts/preflight.py --worker-id blue --node 113.46.17.205:153 \
  --bindings-file /protected/path/container-bindings.json --write-bindings
```

This is read-only with respect to execution hosts and does not enable a pool or
start training. An existing pinned binding is verified rather than rediscovered.
`docker` may also name a shared logical environment. With explicit container
selection, each node pins its container ID, image ID and actual `image_ref` under
that key. Runtime checks both the running container and the node's image reference;
changing either identity requires an explicit new binding. Identical environment
names do not certify identical dependencies or multi-node compatibility.

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
