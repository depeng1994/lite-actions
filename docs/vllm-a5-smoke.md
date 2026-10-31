# Unified Yellow A5 Worker — TorchTitan + vLLM Ascend

This is a **single process** and a **single shared scheduler**. It replaces the
Yellow `ci/a5-32p@11562b96` process after its work is drained; it does NOT start
a second worker. No edits to `torchtitan-npu/vllm-ascend` main are required.

## Deployment invariants

- Use `LITE_ACTIONS_MODEL_REPOS=torchtitan-npu/torchtitan-npu,torchtitan-npu/vllm-ascend`.
  Leave `LITE_ACTIONS_MODEL_REPO` unset in dual mode. The original
  `LITE_ACTIONS_MODEL_REPO=<one repository>` mode is retained for tests.
- Keep `LITE_ACTIONS_STATE_DIR=/var/lib/lite-actions/yellow` unchanged.
  Do **not** clear or rewrite `unified-jobs.json`, quarantines, or jobs.
- Keep the current `LITE_ACTIONS_RESOURCE_LOCK_ROOT` and `LITE_ACTIONS_AGENT_LOCK`
  unchanged (normally both unset to use their established shared defaults).
  One worker process continues to own all `a5_shared_yellow` leases.
- Preserve the complete live Yellow `config/pools.json`, `config/workers.json`,
  verified per-node `node_ip`, pinned container IDs/images, `host_ssh`,
  `force_reclaim` policy, CANN paths, and credentials. The GitHub JSON template
  **does not contain** your live Yellow A5 node inventory.
- `LITE_ACTIONS_ALLOWED_ACTORS` and `LITE_ACTIONS_ALLOWED_BRANCHES` retain
  **TorchTitan** semantics when `LITE_ACTIONS_TORCHTITAN_ALLOWED_* ` is unset.
  Optionally configure the new TorchTitan-specific keys to override them.
  `LITE_ACTIONS_VLLM_ALLOWED_ACTORS=depeng1994` and
  `LITE_ACTIONS_VLLM_ALLOWED_BRANCHES=main` apply **only** to vLLM.
  vLLM pull_request_target additionally requires PR author/actor depeng1994,
  source `depeng1994/vllm-ascend`, base `main`, exact `lite-a5-pr-N-SHA`
  run title, and current PR HEAD SHA match.
- `LITE_ACTIONS_VLLM_ASCEND_SET_ENV_PATH` must be set to the real CANN toolkit
  initialization script available **inside the pinned A5 container**.

## /etc/lite-actions/yellow.env diff

Retain all existing environment entries and secrets, and add just these new
entries (adjust the CANN path to the verified live container):

```diff
+LITE_ACTIONS_MODEL_REPOS=torchtitan-npu/torchtitan-npu,torchtitan-npu/vllm-ascend
+LITE_ACTIONS_VLLM_ALLOWED_ACTORS=depeng1994
+LITE_ACTIONS_VLLM_ALLOWED_BRANCHES=main
+LITE_ACTIONS_VLLM_ASCEND_SET_ENV_PATH=/usr/local/Ascend/ascend-toolkit/set_env.sh
```

If a legacy `LITE_ACTIONS_MODEL_REPO` entry is present, remove it
in dual mode. Existing TorchTitan `LITE_ACTIONS_ALLOWED_ACTORS` and
`LITE_ACTIONS_ALLOWED_BRANCHES` remain unchanged, especially any `ci/`
branch allowlist needed by the already-tested smoke workflows. Do not add a
different resource-lock root or second state directory.

## Safe one-time switch checklist

1. Record the current `/opt/lite-actions/current` release SHA and make
   filesystem backups of current link, Yellow env, pool/worker config and
   `/var/lib/lite-actions/yellow` state. Keep file permissions on secrets.
2. Check active tasks, quarantines and pending reports using the existing
   state file and Yellow service logs. Do not stop during live NPU execution.
3. Checkout the reviewed `ci/vllm-a5-smoke` commit, copy the **live** validated
   Yellow JSON config to this release, and validate with `--worker-id yellow
   --validate` in the same environment as systemd.
4. Confirm 4-node `/etc/hccl_rootinfo.json` inside each pinned A5 container
   is present, valid JSON, and has `rank_count=8` before enabling 32P.
   The executor additionally performs this fail-fast check on every multi-node
   case **before** reclaiming any NPU processes. 8P single-node is exempt.
5. Stop `lite-actions-yellow`, atomically switch the `current` symlink to the
   reviewed release, update the existing env file, then start the SAME service.
   Confirm `systemctl is-active lite-actions-yellow`, one unified router PID,
   and logs show no admission/credential errors.
6. Rerun the full vLLM PR #2 workflow (not just the Waiter), verify A5 8P
   `DISPATCH_SMOKE_PASS`, rank-world-size 8, correct comment and cleanup.
   Also run a new TorchTitan A5 8P smoke to confirm no regression.
7. Do not forcibly delete quarantines to make a test pass. Reconcile any
   allocation by its existing state record and verified remote cleanup.

## Rollback

After verifying no active NPU jobs and preserving any new state/quarantines,
stop the service, atomically restore its previous `current` target and
`/etc/lite-actions/yellow.env`, then restart
`lite-actions-yellow`. **Do not restore the old state snapshot over the live
file**: doing so can erase newer reports or quarantines. Save a snapshot
for forensic review, and reconcile new jobs before restarting the old code.

## Validation limitations

Unit tests and GitHub PR CI verify static routing/contract/rootinfo failure
handling; they cannot certify the actual internal-network Yellow host, its
pinned image, or HCCL hardware. The user executes the production switch.
