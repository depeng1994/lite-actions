# Manual API capture — explicit operator request, not original PR proof

This optional candidate closes the Hosted/shared-filesystem gap without a signing key or inbound service. Disabled unless the reviewed release contains `manual_capture.enabled=true`. MR/PR automatic provenance remains separate and disabled.

## Authorization and evidence

The actual GitHub API `workflow_dispatch` Run supplies the authorized operator, exact repository/control commit/workflow/attempt and nonce association. Its Run ID/attempt/actor/C/workflow plus the digest of the entire intent (PR snapshot, selectors, nonce, expiry) are durably bound together. Another GitHub Run cannot reuse that capture as a new request.

GitCode supplies an independently fetched, one-time Manual wake identity, not a claimed actor or original PR event. Require exact configured repository, run/workflow/path, selected head commit/default master, native attempt 1, accepted actual status, and one completed fixed capture job containing exactly one completed user step. The API's `official_shell` command digest (exact UTF-8, without normalization), shell and working-directory must match reviewed policy. This verifies the command text; it does NOT claim `run.head_sha` proves a missing `workflow_sha`, complete workflow/environment provenance, or GitCode caller identity. Those claims are intentionally not used for authorization.

The dispatch credential must remain confined to the reviewed control job, which never executes PR code. The actual authenticated GitHub operator is the authorization boundary; logs alone are not permission. No additional credential is configured by this code.

## Capture format

The completed step emits exactly one line:

    LITE_GITCODE_MANUAL_CAPTURE_V1 <sha256(canonical-json)> <base64(canonical-json)>

Canonical JSON has exactly `schema=1`, `kind=manual-current-observation`, `captured_at` and the complete `intent`. `captured_at` must equal `intent.bridge.event_created_at` and fall inside the independently read API step time bracket (bounded skew). It is not a PR origin timestamp. The existing intent schema is unchanged; native bridge attempt must be 1 and rerun ID `initial`. Only smoke requests selecting `worker_os_blue` and/or `dispatch_smoke_8p` are accepted, never training/16P.

The live API timestamp wrapper `[YYYY-MM-DD HH:mm:ss.SSS+HH:MM] ` is stripped once with an anchored exact-format match; arbitrary prefixes or repeated wrappers are not accepted as capture records.

Blue retrieves bounded complete pages from the exact run/job/step API, then independently re-reads logs and execution metadata. Duplicated/missing markers, changed execution identity, malformed/oversized data, unknown status or unavailable API fail closed. It does not trust dispatch-supplied copies of the log.

The immutable source key binds repository/workflow/run/job/attempt/pipeline and excludes H/B/selectors. Same source with changed payload is equivocation. First-enrollment age uses the platform step START time conservatively, not a log-provided fresh clock. Fresh time is rechecked after all network reads and the serialized PR authority read, immediately before registration. Original deadline remains mandatory.

## Ledger and execution

Blue independently reads current authoritative PR H/B in the same Coordinator transaction and requires exact equality with the requested snapshot. Previously observed invalidations and selector ownership high-water marks reject stale or unknown late requests; invisible ABA remains unprovable. Known revocations survive return to an earlier H/B and process restart. Negative terminal origin states and changed retained evidence create durable revocation.

Registration grants no execution: existing complete Prepare/artifact/manifest/source/slot verification is still required. Hosted Prepare can build its immutable Plan without a Blue generation; early registration needs no extra ingress Check. Hosted runners never create a replacement Blue authority ledger.

Historical `source_attestations` storage is reused as a generic retained-evidence table, but these rows are explicitly `manual_api`, with `captured_at`, `request_started_at` and actual GH delivery metadata. They contain no invented workflow source SHA or original PR-event timestamp. The `manual-api:` key namespace identifies policy, not a cryptographic key.

## Configuration and rollback

`manual_capture` requires enabled, workflow_id, workflow_path, job_identifier, step_identifier, step_name, script_sha256, shell (`bash`), working_directory (`""` or `"."`), and request_authority (`github_dispatch_operator`). Optional age/skew/status bounds are defined by ManualCapturePolicy. It cannot be combined with signed attestation mode. A final model capture workflow and its actual API command digest must be reviewed before filling these values; the old 42-field context probe is not a capture and cannot enroll.

Coordinator state version is **6**, because old v5 binaries do not understand manual-origin revalidation. v5→v6 preserves authority/intent/evidence, leases, outbox and attempt history; v4 and earlier supported migrations remain atomic. Old binaries refuse v6 startup and authority/mutation transactions; diagnostic read-only queries are not execution authority. Activation must first drain/reconcile and stop the old single writer; never run a second Blue or migrate beneath an open old transaction. Rollback needs a v6-compatible reviewed binary. Never lower user_version, delete binding/ledger, or restore an old database that loses revocations, leases or outbox entries.

No deployment, credential change, hardware lease or live Forward acceptance is claimed by CPU tests. GitCode original PR automation and actual manual end-to-end return status require separate live validation.

## Candidate verification

Local and independent full regression: 471 tests PASS, including state-v6 rollback barriers, atomic failed migration, platform timestamp framing, stale-age/deadline rejection, replay/equivocation/ABA and real Producer→Reader→Broker integration. Pinned Ruff 0.17.3 lint/format, compile and diff checks pass. Local log: `/root/gitcode-forward-work/tt-worker-manual-v6-final-regression.log`.

The strong validator deliberately re-reads complete run/job/log evidence on current checks. Its approximately 20 GitCode HTTP requests per origin check (before additional PR/GitHub reads) must be measured against the existing 15-second current-authority proof lifetime. CPU tests are not latency evidence; no cache, longer proof TTL or relaxed source semantics has been introduced to claim success.
