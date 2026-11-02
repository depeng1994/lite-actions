# vLLM GitHub-native Blue CI

This release path does not require a GitCode vLLM repository or GitHub Checks App. TorchTitan's separate GitCode/schema-4 release gates remain unchanged.

## Entry and compatibility

The trusted controller is `cann-framework/vllm-ascend` on `main`. A PR must originate from `depeng1994/vllm-ascend`, be open, non-draft and authored by `depeng1994`. Label `lite-a3-blue` opts into the Blue OS probe plus A3 8P; `lite-a3-16p` adds the required two-node 16P case. Unlabelled PRs and empty manual selections retain the existing Yellow A5 selection contract. Yellow service deployment is outside this release.

Manual Blue smoke uses fixed `test_id` selectors, for example:

```json
[{"test_id":"vllm_worker_os_blue"},{"test_id":"dispatch_smoke_a3_8p"},{"test_id":"dispatch_smoke_a3_16p"}]
```

Do not mix Blue and Yellow cases in one request. Blue 8P/16P always select `a3_shared_blue` and the actual Docker container `vllm_hccl_smoke`. The OS probe does not fetch model code, enter Docker or acquire NPU devices.

## Immutable source and controller

The schema-3 request and existing A5 wire fields are preserved. Blue adds a separate `lite-ci-source-<attempt>` artifact containing an exact plan-digest-bound source record. This is not the GitCode schema-4 protocol.

For PR tests the producer verifies the real Head H and current target-branch Base B, obtains a platform merge commit M, checks ordered parents `[B,H]`, records tree T, and statically checks the approved source payload interface. Neither Prepare nor the Worker control domain executes PR Python. Control checkouts use `github.workflow_sha` C, not a floating `main` checkout. Worker admission independently requires C in the trusted deployment setting `LITE_ACTIONS_VLLM_CONTROL_SHAS`.

The request is uploaded last. Blue requires the exact run/attempt Prepare job to finish successfully before admission. The Worker downloads and validates the source sidecar, repository IDs, H/B/M/T/C and current PR authorization. The actual Git archive is built from M, checksum-verified in the cache and after staging; results retain H in the original schema-3 `sha` field and carry the explicit native source record. Manual commit smoke is labelled `mode=commit` and is never represented as a PR merge test.

The trusted HCCL supervisor is supplied separately from the immutable Worker release. Only `.github/scripts/lite_actions/blue_hccl_payload.py` from the tested source executes in the NPU container. Its API is `PAYLOAD_API_VERSION=1` with `all_reduce(tensor)`. The trusted supervisor owns topology, timeout, expected sums 28/120, runtime source path and evidence. HCCL smoke does not certify complete vLLM inference, and optional submodules such as catlass are not materialized for this payload.

## Version invalidation

PR synchronization uses per-PR GitHub concurrency. Blue independently checks Head, current Base, target ref, labels/specification, open state and run attempt while queuing/running, immediately before launch, and before publishing PASS. A stale task is cancelled and cannot publish a current PASS. The final model-side result step checks the same version and its own active run before writing the optional `Lite-Actions-Blue-Merge` commit status.

A default-branch push or the bounded ten-minute reconciliation workflow invalidates the old status before dispatching a new exact H/B recheck. Its pending status is a durable dispatch-intent claim. If the dispatch response is lost, the reconciler does not blindly repeat POST or infer success; inspect the nonce-bound run and explicitly rerun an entire trusted PR intent if necessary.

The status is `versioned_only`: its description includes the tested Base and required-case specification. It is not an atomic merge authorization. No Required Checks or branch protections are changed. Event/polling observation has a race window; no zero-window guarantee or unobserved A-to-B-to-A detection is claimed.

## Resources, containers and recovery

One Blue process polls both model repositories and uses the same host physical-device/port locks. A container name never creates a second lock domain. Original quarantines remain effective, including any devices visible inside the vLLM container.

Before launch, the Worker verifies and persists each node's actual container name, immutable ID, image ID/reference, host and device allocation. Stage, run, log collection, cancel and recovery use that captured ID, never a replacement container found by name. Per-job process markers and PID start times prevent unrelated process termination. Recovery uses persisted runtime/environment data and does not fetch or import model source. An old record without sufficient cleanup information requires manual reconciliation rather than guessing or clearing its quarantine.

A release must drain the old Worker, snapshot persistent state/configuration, retain the shared lock root and quarantine records, and start only one candidate. Test evidence must record exact model/Worker commits, run/attempt, H/B/M/T/C, per-node container IDs, HCCL results and cleanup. Rollback must retain newer state/outbox/quarantines; do not restore a stale state snapshot over completed cleanup information. Container replacements must retain the previous stopped container for an explicit rollback, not delete unknown workloads.

## Validation status

This document describes the implementation contract. CPU/negative regressions and workflow lint are separate from live hardware acceptance. Actual deployment and live run evidence must be recorded in a dated release checkpoint; this file alone is not a PASS declaration.
