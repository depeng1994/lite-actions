# Worker Onboarding & A5 Troubleshooting Checklist

Use this before adding a new A5 execution environment. Distinguish the Lite Actions control-plane Dispatcher from the accelerator resource pool: a named CPU smoke worker is not the same thing as an A5 training node.

## 1. Decide which layer you are adding
- Dispatcher / Host Server: receives GitHub Actions requests, validates them, schedules work, records state, and reports results.
- Named worker registry (config/workers.json): intended for explicitly addressed lightweight worker probes, such as the depeng OS smoke.
- Accelerator resource pool (config/pools.json): defines A3/A5 hosts, device IDs, verified allocation groups, SSH aliases, HCCL topology, and concurrency limits for training workflows.
- For an A5 workflow, adding only an entry to workers.json is insufficient. Add and validate the A5 resource-pool topology and workflow profile.

## 2. SSH aliases and execution context
- Verify every alias from the Dispatcher account's SSH config; do not copy alias names from another machine.
- Keep execution/container SSH separate from host/PID namespace SSH and notification/broadcast SSH. They may point to different namespaces and mount sets.
- Check non-interactive SSH, host-key verification, hostname, user, Python, shared filesystem mounts, and model/checkpoint readability.
- Do not disable host-key verification. Prefer HTTPS for fetching public GitHub source if the machine lacks GitHub SSH host keys.
- Test the exact command used by Dispatcher, not just an interactive login shell.

## 3. CANN and NPU preflight
- Configure the actual ASCEND_SET_ENV_PATH for the target environment; never assume an A3 Toolkit path exists on A5.
- Source the configured Toolkit before running npu-smi. Otherwise missing libraries (for example libc_sec.so) can make a healthy host appear unavailable.
- Run preflight through the execution alias that has CANN mounted. Host-PID cleanup must still use the host namespace alias.
- Validate the NPU process-table parser and device-index mapping against real output on every target type.
- Distinguish a busy candidate from a failed probe. SSH failure, timeout, missing library, or malformed output is an infrastructure error, not proof that the hardware is busy.

## 4. Device numbering and topology
- Explicitly establish whether IDs are physical cards or card-die/chip IDs. Do not assume device 0-7 means the same thing on every platform.
- Verify device_ids, allocation_groups, process_indexing, node IPs, network interface, and per-host capacity.
- Add only operator-verified groups to allocation_groups. Add multi-node bundles to verified_multinode_allocations only after real distributed validation.
- For A5 64P (8 nodes × 8 devices), verify every node and the actual cross-node HCCL path before enabling the pool.

## 5. Occupancy, cleanup, and stale state
- Query the exact candidate device group. A host can have one busy group and another free group; the scheduler must continue to later verified candidates.
- Never kill unknown processes to make a smoke pass. Reclaim only explicitly approved process classes after validating host PID, process start time, command class, and PID reuse.
- A training process exit code of 0 is not sufficient. Verify remote children are gone and devices are released before reporting PASS.
- If cleanup is uncertain, report infrastructure failure and quarantine the allocation; do not leave it marked successful.
- Reconcile stale running/blocked_recovery records using remote process evidence, not just elapsed time.

## 6. Locks, ports, and parallelism
- If multiple Dispatchers can access the same hardware, configure one shared resource-lock root. Per-checkout locks do not prevent cross-machine double allocation.
- Acquire all device leases, per-host capacity slots, and HCCL port ranges atomically for the entire allocation.
- Check CPU, memory, disk, staging bandwidth, and HCCL ports as well as NPU availability.
- Define whether the A5 workload permits parallel jobs before choosing max_jobs_per_host.
- Cancellation must stop waiting/launching and must not leave remote children or stale leases.

## 7. GitHub Actions and result protocol
- Keep the workflow filename exact; this repository uses underscore-separated names such as a3_8p_lite_actions.yml.
- Match the workflow's test_id/suite/resource request with the trusted Dispatcher pipeline config.
- Confirm the intended repository, branch, and actor are allowlisted in the Dispatcher environment.
- Keep run ID, attempt, commit SHA, pipeline ID, test ID, and allocation consistent from request through result comment.
- Fetch/checkout the requested commit SHA. Verify cached source archives against that SHA and publish cache entries atomically.
- End-to-end acceptance is GitHub trigger → immutable request → Dispatcher pickup → resource allocation → remote execution → verified cleanup → result comment → Waiter accepts the exact run/attempt/SHA/test ID.

## 8. Minimum A5 enablement gate
1. SSH, host-key, hostname, non-interactive authentication pass for all eight nodes.
2. CANN loads and npu-smi succeeds on every execution alias.
3. Device IDs and process indexing are verified on every node.
4. Python dependencies and model/checkpoint/data mounts are readable.
5. Node IPs, network interface, HCCL ports, and cross-node connectivity are verified.
6. A per-node OS/device probe passes.
7. A real distributed smoke validates world size and an all-reduce result.
8. Remote process cleanup and NPU release are verified.
9. Waiter accepts the exact result and rejects stale or wrong-worker results.
10. Unit tests, workflow contract tests, and one real GitHub Actions E2E pass. Record queue delay, allocation/preflight, execution, cleanup, and end-to-end duration separately.

## A3 incidents that exposed these pitfalls
- The resource pool referenced SSH aliases that did not exist in the Dispatcher account.
- Host relay and Docker execution alias have different CANN mounts and PID namespaces.
- npu-smi without sourcing Toolkit failed with missing libc_sec.so.
- On A3-4, devices 0-7 were occupied by existing VLLM workers while devices 8-15 were free; the scheduler needed to probe each verified group rather than treating the entire host as busy.
- One run printed DISPATCH_SMOKE_PASS but was reported INFRA_ERROR because cleanup/release verification failed. The GitHub Actions run—not only the inner smoke log—must finish successfully.
- Keep the A5 64P pool disabled until the actual 8-node topology, cross-node HCCL, cleanup, and capacity policy are verified.
