# Lite Actions 2.0 validation record

## Code under validation

Two independent workers share the common router/scheduler/executor. Schema-3 producer
and consumer contract sources are identical. Workflow defaults support Python field
coverrides, and the two actual DSV4 8P definitions are used unchanged.

## Automated checks

- Model repository: 44 tests and 24 subtests passed on Python 3.12 before final cleanup.
- Dispatcher: 99 tests and 61 subtests passed; five Linux/environment-specific tests
  are deferred to the worker. Final Linux rerun is required after all code changes.
- New regression coverage includes worker routing, source/plan identity, FIFO,
  disjoint concurrent leases, host capacity, quarantine, cancellation, restart,
  outbox retry, canonical SSH aliases and Unicode/JSON byte budgets.

## Deployment preflight

- ycy and hwy both reached GitHub API and github.com.
- Current physical A3 is owned exclusively by blue in the new inventory.
- Canonical aliases on ycy were registered using the existing verified SSH host keys.
- `113.46.17.205:153`: requested image exists, with two running container candidates;
  explicit selection is required before training.
- `113.46.17.205:154`: requested image is absent. The 16P hardware gate is pending.
- Yellow training pools are disabled; OS probe is the first-release acceptance target.

## Required live acceptance

1. Blue and yellow OS probes through immutable GitHub plans.
2. 8P smoke and both five-step DSV4 cases on a pinned verified environment.
3. 16P smoke after the second node's environment is ready.
4. Two simultaneous users/runs, cancellation, cleanup, restart and report retry.
5. Actual GitHub results: author/worker/SHA binding, <=20 log lines, byte limits.
6. PR automatic execution and removal of obsolete Lite Actions entrypoints.

No NPU training PASS is claimed by this record until the corresponding live evidence
is appended. Existing historical production logs are preserved outside the new source.

## Reviewed implementation and live GitHub evidence

The independent static review identified and fixed: unbound-node admission, log
collection touching unallocated nodes, superseded-attempt cancellation, admission
crash recovery, and release proof based on NPU idleness alone. The release proof
now also requires durable launch/completion evidence and absence of marked processes.
NPU execution requires root PID visibility; yellow's first-release scope is OS-only.

- Model branch, after current-upstream integration: **54 passed, 24 subtests**.
- Local dispatcher regression: **103 passed, 6 environment-specific skips, 61 subtests**.
- Blue Linux reviewed build: **109 tests, OK, 1 skipped** (adjacent model checkout).
- Yellow's full-PID proof regression requires blue/root privileges; its earlier
  rootless failure was fail-closed on an unreadable `/proc` entry. That root-only
  acceptance runs on blue; yellow OS routing does not use NPU cleanup.

Manual OS E2E: https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41040977540

| Worker | Result | Diagnostic lines | Comment bytes | Serialized request bytes |
| --- | --- | ---: | ---: | ---: |
| blue / ycy | PASS | 13 | 872 | 988 |
| yellow / hwy | PASS | 18 | 1172 | 1306 |

The run executed model commit `5e84f6610064a7092cc596a9f4c3bb64e521ed8c` with the
reviewed dispatcher candidate, schema 3 Artifact, two independent worker pickups,
Commit Comments and successful exact-identity Waiters.

PR #11 automatically triggered both workflows. OS cases passed. NPU cases returned
`POOL_DISABLED` because candidate verification deliberately disables resource execution
while the existing release remains responsible for real hardware. The aggregate checks
correctly failed instead of falsely reporting an unexecuted training PASS.

Draft changes:
- https://github.com/depeng1994/lite-actions/pull/10
- https://github.com/torchtitan-npu/torchtitan-npu/pull/11

## Remaining gates

The requested image is present on `:153` in two containers; binding selection is
pending. The requested image is absent on `:154`. No DSV4 or NPU smoke PASS is claimed.
The original release has one quarantine record, which must be preserved/mapped and
reconciled before hardware ownership/lock migration; candidate OS verification does
not clear it or take its devices.

Full pre-commit has **not passed**. Local syntax/YAML/format/spelling/architecture
checks passed; OAT processing and project-wide Pyrefly remain unsuccessful in the
local stack. A matching Linux retry was blocked by its old system Git lacking
`git ls-files --deduplicate`. These failures are recorded, not waived or reported green.
