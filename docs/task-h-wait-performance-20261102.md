# Task H — wait-gitcode performance findings and isolated SDK patch

## Scope and safety

- Source: SDK `0529d4de78cab357e9ab4aea1566ed6f2f288907`.
- Independent worktree only: `task-h-wait-performance-20261102`.
- No live tree, control owner tree, release, deployment, dispatch, cancellation, SSH/NPU execution, or credential access.
- The separate control baseline `b77ed30a9ce3898db984698e6ee0d99aaf11b195` was read-only.

## Measured timeline (UTC)

| Event | Time | Evidence class |
|---|---:|---|
| Prepare starts | 14:35:17 | supplied run timeline |
| Prepare succeeds | 14:37:07 | supplied run timeline |
| wait-gitcode starts | 14:37:10 | supplied run timeline |
| Hosted emits authenticated-result envelope with `test_result=PASS` | 14:44:19.6405163 | retained terminal log evidence |
| wait-gitcode ends | 14:44:31 | supplied run timeline |
| GH run succeeds | 14:44:40 | supplied run timeline |
| GC reports COMPLETED | 14:45:34 | supplied run timeline |

The wait job therefore spans 441 seconds; Hosted emitted PASS 428.64 seconds after its start, leaving about 11.36 seconds between the Hosted PASS line and the reported wait-job end. The retained evidence does **not** include GH job/step logs or an explicit timestamp for the OS process exit, cleanup acknowledgement, receipt consumption, or authority proof consumption (`proof_consumed_exact_timestamp=NOT_EXPLICITLY_OBSERVED`). The GC terminal log records the manual-forward `GITHUB_RUN_BOUND` and terminal response, not internal wait-loop iterations.

This is not evidence that the 441 seconds were polling sleep. The wait job's purpose is to await the actual worker execution and then validate its receipt, aggregate and current authority. The real worker run may account for most of the interval; exact decomposition is UNKNOWN from the retained logs. The Hosted PASS timestamp is the earliest evidenced completed aggregate result, not proof that all terminal validations had completed at that instant.

## Call DAG and repeated work

Production admission/start path:

1. `admit`: early intent + PR authority refresh; authenticate source event; load immutable request; refresh again after materialization; verify exact plan; atomic claim.
2. `guard/startable_source`: verify exact Plan/source event; read exact GitHub run/attempt; refresh PR authority; assert current execution generation; load stored execution.
3. `work`: guard, atomic case claim, stage resources.
4. `before_launch`: after staging, re-verify Plan/source event; re-read exact GitHub run/attempt; refresh PR authority; assert generation and lease/fencing token immediately before launch.
5. `finish(PASS)`: fresh guard again before accepting PASS; release/cleanup verification; persist case result.
6. Aggregate publication and Hosted waiter each perform their own terminal checks. These are separate freshness boundaries and are not cached across phases.

For the concrete production `ObservedGitCodeAuthority`, `verify_plan()` calls `verify_event()`, which freshly re-observes the PR through `_observe()` inside the shared Coordinator transaction and reconciles the observed authority/revocation. The immediately following `refresh()` was therefore a second PR observation in the *same guard*. Previously this cost a further set of six GitCode authority reads per guard (as reported in the handoff); `actual_run()` remains a separate required GitHub observation. Generic callbacks and providers not proven to share the exact Coordinator/policy do not get this optimization.

## Implemented isolated SDK changes

1. `GitCodeAdmissionBroker.assert_plan_current()` now returns a boolean only when the exact concrete `ObservedGitCodeAuthority` is bound to the same resolved Coordinator database and exact reader policy. The method still requires a valid `EventProof`; missing/generic providers fail closed or return false as before.
2. `startable_source()`, the post-materialization claim boundary, `before_launch()`, and aggregate flush guard skip the immediately redundant `refresh()` only when that just-completed authenticated Plan verification itself made the fresh shared observation. Otherwise the old explicit refresh remains.
3. `actual_run()` and Coordinator startability/lease/fencing checks remain. No result, proof, authorization, or TTL is cached across guards or stages.
4. `GitCodeClient` emits sanitized per-request timing logs: method, path (without query), HTTP status, retry attempt, elapsed milliseconds. No headers, bearer token, environment, response body, or signed URL are logged.

## Security invariants retained

- Cancellation/revocation: every Plan verification still performs the fresh event/source observation; revoked authority is persisted by the existing provider transaction.
- ABA/base/head changes: fresh observation still compares the complete authority snapshot and updates revisions/invalidation.
- Run/attempt identity: `actual_run()` remains in each startability/launch guard.
- Late proof/aggregate: no cached proof is introduced; Hosted still performs its existing final freshness check.
- Cleanup and lease fencing: lease-active checks before launch and cleanup/release checks at finish remain.
- Timeout/deadline: no deadline or challenge expiry extension; no sleep/timeout semantics changed.
- PASS: no hard-coded PASS or weakened identity/aggregate checks.

## Validation

- `compileall` passed before the full suite.
- Focused regression set after the logging compatibility fix: 64 passed in 2.41s.
- First full-suite run: 512 passed, 1 failed because the test's mocked `BytesIO` response has no `.status`; corrected instrumentation to default a missing mock status to HTTP 200 (real HTTP responses still supply status). The full suite is being rerun after that correction.
- No GitCode/GitHub network probes, OS/NPU runs, device leases, or external dispatches were performed.

## Performance interpretation and next measurement

- The guard optimization removes one duplicate PR refresh from each eligible same-guard sequence; with the previously observed six GitCode reads per refresh, expected saving is up to six GitCode HTTP calls per such guard. It does not eliminate the genuinely fresh observations between admission, launch, finish, aggregate publication, and Hosted final authority.
- Per-request timing logs now allow API latency and retry/backoff to be separated from worker execution on the next PM-owned integrated run.
- The wait action currently polls at 15 seconds. Reducing this to 1–2 seconds would multiply artifact/Check/authority reads by roughly 7.5–15x and can only reduce post-completion detection latency by at most about 13–14 seconds; it cannot explain or remove a 7m21 execution interval. Keep the interval unchanged until wait-loop metrics show polling delay is material.
- Required control-side instrumentation (not modified here): log monotonic elapsed time for each `reader.load`, `waiter.poll`, and sleep; poll sequence number; outcome/reason; and final exit reason. Pair these with the SDK HTTP timing events and GH step timestamps. Do not log request bodies, environment, headers, credentials, artifact URLs, or signed URLs.

## Target budget (proposal, not measured SLA)

- SDK request budget: remove the known same-guard duplicate refresh; do not cap or suppress required freshness reads.
- Waiter overhead after aggregate completion: aim for <=15 seconds including polling wake-up and final validations; measure before tightening the poll interval.
- OS smoke: 3 minutes remains a product target, not established by this evidence. The 7m21 sample does not prove a 7m21 fixed lower bound, but it also does not support a claim that poll tuning alone can reach 3 minutes. Need per-step OS logs and wait-loop timings from a PM-owned run.

## Second-pass completion (2026-11-02)

G's full DAG review was incorporated. The first pre-registration admission guard was also redundant for the concrete provider: `verify_event()` itself executes `_observe()` and commits the current PR authority/revocation to the same Coordinator. Added `observe_event_current()` so `admit()` skips its preceding `refresh()` only when `verify_event` and `verify_plan` are bound to the same exact concrete `ObservedGitCodeAuthority`, same resolved Coordinator path, and same reader policy. Generic callbacks, cross-database providers, cross-provider callbacks, and policy mismatch retain explicit refresh. The post-materialization admission guard remains a separate new observation; it independently verifies Plan and refreshes only when its own concrete verification did not already observe authority.

Additional regression coverage:

- Real `GitCodeAdmissionBroker.observe_event_current()` with concrete provider: one fresh authority snapshot, no second explicit refresh.
- Real `GitCodeAdmissionBroker.assert_plan_current()` with concrete provider: exactly one new authority snapshot per call; repeated guard invocation performs another observation (no cross-stage cache).
- Real `startable_source()` entry: one fresh authority snapshot plus the exact GitHub Run read; duplicate refresh spy is not called; second entry repeats both observations.
- Generic callback path: explicit refresh/read-authority and event-proof callbacks are called.
- Cross-DB, cross-policy, and wrong-reader-policy capability checks refuse the optimization.
- Runtime regression instruments repeated `before_launch`, `finish(PASS)` and aggregate guard calls: every guard re-verifies and re-reads GitHub Run, while same-guard duplicate refresh is absent.
- Negative runtime regressions revoke the GitHub Run between staging and launch and after execution before finish; no NPU-case PASS is accepted. Existing aggregate cancellation test verifies a canceled GH Run cannot publish current aggregate success. Existing suite retains PR base/head ABA, cleanup, lease fencing, receipt and timeout tests.

Second-pass SDK validation: `ruff` passed on changed SDK files; `compileall` passed; full suite **519 passed in 47.83s** (final run).

## OS execution duration evidence

Reviewed the retained `manual-os7-terminal-safe-evidence-20261102.json`, `manual-os-68ad-final-safe-evidence-20261102.json`, `os4-gh41438604990-cancel-journal-20261102.json`, and `retained-current-parallel-9gc1gh-latency-20261102.json`. The OS7 evidence preserves GitCode manual-capture/forward job and Hosted aggregate-result timing, but its GitHub selected log collection is empty and it has no authenticated OS process launch/exit or cleanup timestamps. The other inspected evidence is a cancelled wait job or API-read latency sample, not an OS process journal. Recursive search of retained top-level JSON evidence found no OS process start/end timing fields. Therefore **actual OS execution duration is UNKNOWN**; do not infer it from the 7m21 wait-job wall clock or Hosted PASS timestamp.
