# Native Smoke pilot: evidence and operation

This is an opt-in Blue pilot on one scheduler. GitCode and GitHub independently
submit their own immutable plans; neither platform dispatches the other. Both
plans enter the same worker's persistent queue and use its physical locks.
This pilot covers trusted-branch Smoke only, not PR/Fork execution.

## Deployment

The tested worker source is `53712f3a7f7c627ba26c3eea38f87562bd6ac18c`,
installed under `/opt/lite-actions/pilots/native-53712f3`, with
`/opt/lite-actions/native-pilot` pointing to it. The systemd unit is
`lite-actions-native-pilot.service`; the previous `lite-actions-blue.service`
is stopped and disabled under the operator's explicit instruction. The unit
uses the original `/etc/lite-actions/blue.env` for credentials and
`/etc/lite-actions/native-pilot.env` for the non-secret pilot overrides. It
shares `/var/lib/lite-actions/blue` and
`/root/.cache/lite-actions/resource-leases`. The service file and an example
of the overrides are in `deploy/`.

There must be one scheduler for this state and pool. Stopping the legacy
service before starting this service is required; two independent schedulers
sharing only physical locks do not safely share quarantine or FIFO state.
On rollback, first wait for all pilot cases to finish and verify cleanup; stop
and disable the pilot before enabling the previous service. Retain the state
file, inspect any new quarantines, and verify the older release's capabilities
and branch configuration. Do not assume rollback preserves the pilot FIFO.

## Real platform and NPU results

| Run | Source revision | Observed result |
| --- | --- | --- |
| [GitCode zero-device probe](https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/5928216a1f0847f48ba3ecbefbe4f811) | `440f77d58867fb55d806eeaf55f0c2abcc460e8b` | Checkout, Python setup, catalog upload, plan receipt, worker probe and result completed successfully. |
| [GitCode Smoke 8P / 16P / 8P](https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/a70e5d146e404ac1af01ae682c45f91c) | `62ede58c92bea40fbf7f228d78d3fc0e871ee35a` | All three cases reported PASS with verified cleanup. All-reduce sums were 28, 120 and 28. |
| [GitHub cross-platform trial](https://github.com/cann-framework/torchtitan-npu/actions/runs/42674109423) | `6a54a59b6980917a9eb6496b6af688a241473c72` | All three hardware cases reported PASS and verified cleanup on the worker, but GitHub registered only one Matrix Job and its reusable-workflow result was `abandoned`. The overall workflow failed; this is **not** GitHub platform acceptance. |

At `2026-11-10 02:30:40` China time, [a worker state snapshot](native-smoke-fifo-snapshot-20261110.json) showed GitCode
cases admitted as sequence 2, 3 and 4. GitHub's sequence 5 was staging on
node `:153` devices `0–7`; sequence 6 (16P) and sequence 7 (8P) were queued.
GitCode sequence 3 still held `:153` devices `8–15` and `:154` devices `0–7`
during cleanup, while `:154` devices `8–15` were free. Thus the later GitHub
8P could fit in the free group but did not bypass the queued 16P. This is one
real same-pool contention observation, not proof about different pool names.

The historical entries in the workspace design document predate this pilot
and must not be counted as results of the new implementation. Additional
failure, cancellation, restart, platform and hardware cases remain subject
to the acceptance matrix in that document.
