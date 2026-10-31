# vLLM Ascend A5 Smoke onboarding

The model repository is `torchtitan-npu/vllm-ascend`. Its single
`.github/workflows/lite_actions_dispatcher_smoke.yml` is a default-branch
`pull_request_target` controller, *not* an NPU executor. Untrusted PR code is
never run in its GitHub-hosted Prepare or Waiter jobs.

## Cases

- PR from `depeng1994/vllm-ascend` to `main`, `releases/*`, or `*-dev`:
  run `dispatch_smoke_a5_8p` (one node, eight NPUs) automatically.
- PR label `lite-a5-32p`: additionally run `dispatch_smoke_a5_32p`
  (four nodes, eight NPUs each).
- Manual dispatch on default `main`: pass
  `[{"test_id":"dispatch_smoke_a5_32p"}]` to test just 32P.
- All results are schema-3, tied to PR head SHA, Run ID, Attempt and case.
  The Worker denies stale PRs and any source outside the allowlist.

## Yellow-side steps (operator only)

Do **not** modify the existing TorchTitan deployment state or process.
After selecting the tested `ci/vllm-a5-smoke` Lite Actions commit and checking
it has been deployed on the Yellow host, start a **second** Yellow process
using the *same physical device/host/port lock root* as the original process,
but its own state and agent locks:

```sh
export LITE_ACTIONS_MODEL_REPO=torchtitan-npu/vllm-ascend
export LITE_ACTIONS_ALLOWED_ACTORS=depeng1994
export LITE_ACTIONS_ALLOWED_BRANCHES=main
export LITE_ACTIONS_RESULT_AUTHOR=depeng1994
export LITE_ACTIONS_STATE_DIR=/var/lib/lite-actions/vllm-a5-yellow
export LITE_ACTIONS_AGENT_LOCK=/var/lock/lite-actions-vllm-a5-yellow.lock
# MUST use the same shared lease directory as the already-running Yellow process.
# If the original worker leaves this variable unset, leave it unset here too.
# export LITE_ACTIONS_RESOURCE_LOCK_ROOT=<original-worker-value>
# Set to the verified CANN toolkit path INSIDE the pinned A5 container:
export LITE_ACTIONS_VLLM_ASCEND_SET_ENV_PATH=/usr/local/Ascend/ascend-toolkit/set_env.sh
# Set authorized GitHub credential in the protected process environment.
python3 unified_router.py --worker-id yellow --validate
python3 unified_router.py --worker-id yellow
```

The configuration under `config/pools.json` must be the operator-verified
Yellow production configuration: `a5_shared_yellow.enabled=true`, verified
`node_ip` (HCCL fabric IP, not management IP), SSH aliases, physical devices,
`network_interface`, and exact `vllm-kimi-wangjinyin` container/image binding.
The public `main` JSON template has zero A5 nodes: **do not start from it**.

Before enabling a real job, verify: 1x8 HCCL smoke, source SHA staging,
locked device selection, exact container image and CANN runtime, teardown,
and failure/cancel behavior. The 32P test additionally requires 4 reachable
HCCL nodes and complete multi-node connectivity.

The private state dir and unique agent lock prevent one process from claiming
the other's in-memory work. Device, host-budget, and port leases are shared
through the common `LITE_ACTIONS_RESOURCE_LOCK_ROOT` to avoid collisions.
Any manual `LITE_ACTIONS_RESOURCE_LOCK_ROOT` override must be set identically
in the already-running TorchTitan worker; otherwise **do not enable the
second worker**, as NPU allocation races are possible.

## PR workflow deployment order

The controller is loaded from **organization main**, never from a feature or
release branch. Merge its reviewed integration PR into
`torchtitan-npu/vllm-ascend:main`, then open a separate ordinary test PR from
the personal Fork. Merely opening a PR that introduces the controller does
not activate `pull_request_target` until the controller is in default main.

Keep upstream-sync operations non-destructive: merge upstream to the org
main through a reviewed PR; never force-reset main and delete this Workflow.
