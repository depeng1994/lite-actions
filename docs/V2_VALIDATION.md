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
