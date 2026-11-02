# GitCode emitter attestation — disabled candidate

This module is a code-only protocol candidate. No dedicated persistent key has been generated, installed, uploaded, or enabled. Synthetic tests create temporary RSA keys and delete them. Signing and automatic GitCode Action execution remain disabled until the real original-event payload/provenance and runtime fields are verified and the user approves separate signing-key provisioning.

## APIs and trust boundary

`create_envelope(intent, original_event=..., run_id=..., run_attempt=..., capture_job_id=..., run_head_sha=..., run_head_branch=..., platform_event=..., workflow_id=..., workflow_path=..., workflow_source_sha=..., key_id=..., issuer_repo_id=..., actor=..., issued_at=..., signer=...)` signs domain-separated canonical JSON with RSA/SHA256. A dedicated signer may use the existing OpenSSLSigner implementation, but MUST NOT use the Checks App private key. The trusted target-workflow code must supply ORIGINAL event metadata; the helper never invents it from current time or a current PR GET.

`verify_envelope(envelope, intent, policy, now=...)` uses only the pinned public key on Blue and returns VerifiedAttestation. The attestation contains the exact full intent and digest, source PR snapshot and selectors, control SHA/workflow/pipeline, original event ID/time/snapshot identity, GitCode run ID/attempt, original CAPTURE job ID, runtime head SHA/branch, API platform-event name, emitter workflow ID/path/source SHA, issuer key ID/repository/actor, nonce and issue time. Run head SHA, PR Head and trusted workflow source SHA are separate values and are never inferred from each other.

GitCode run/workflow IDs are bounded strings, matching observed v8 IDs, not GitHub numeric IDs. Original event times are epoch seconds in this candidate schema. Observed run start_time in milliseconds is NOT an original event timestamp and must not be substituted or automatically converted into one. Runtime event kinds and API uppercase platform-event enums are separately pinned. Platform run status is checked by the authenticated adapter, with candidate allowed statuses defaulting to RUNNING.

Original-event age controls acceptance. Refreshing issued_at cannot refresh an old event. Missing original metadata, mismatched full intent or snapshot, future timestamps, stale original timestamps, wrong key/fingerprint/workflow/source/repository, bool-as-integer values and malformed/oversized JSON/signatures fail closed. The key fingerprint is SHA256 of the exact reviewed SPKI PUBLIC KEY PEM file bytes, not an SSH fingerprint or a private key hash. Private-key PEM files are rejected by the verifier.

Signature validity alone is NOT permission to run. The platform ledger must also enforce authenticated current API identity/status, durable exact-envelope enrollment, replay identity, observed revocations, late original-event rejection and current canonical publication ownership. No hidden ABA or native atomic merge guarantee is claimed.

## Optional assembly configuration

Top-level `attestation` is absent or `{enabled:false}` by default. Enabling in a future reviewed release requires:

- enabled, key_id, public_key_file, public_key_sha256;
- issuer_repo and issuer_repo_id (may differ from the PR target);
- head_repo_id (must be the target or explicitly allowed by ControlPolicy);
- workflow_path, workflow_source_sha, workflow_id;
- allowed_event_kinds and allowed_platform_events lists.

Optional limits: allowed_run_statuses (default RUNNING), max_original_event_age_seconds (default 300, maximum 3600), clock_skew_seconds (default 30, maximum 60). These are trusted deployment values, never PR/dispatch inputs. The only key path in Blue policy is a public-key path. The issuer API reader uses the existing separately authorized GitCode read identity. No signing-key storage is configured by this JSON.

For a cross-fork PR the signed target/base repository must equal the control policy target; the signed Head repository must equal the explicit head_repo_id pin. This does not add wildcard fork trust.

## Validation evidence

14 tests pass with temporary synthetic RSA-2048 keys: valid signatures, separate run/workflow/PR heads, original-age replay prevention, missing/invalid original metadata, exact fork pins, changed selector/control/nonce/payload, wrong key/workflow/event, expiry/future checks, duplicate/oversized JSON, private-key rejection and clean verifier subprocess environment. This is cryptographic CPU evidence, not live GitCode Action provenance or permission to enable the workflow.

## Source capture replay identity

VerifiedAttestation.source_key is computed from issuer repository ID, workflow ID, original CAPTURE job ID, GitCode Run ID, attempt and pipeline. It excludes H/B, selectors, nonce, event ID and issuer key ID. Distinct signed payloads with the same source_key MUST be rejected as equivocation by durable enrollment, including across key rotation; a new nonce/event ID cannot bypass this record. Missing CAPTURE job identity fails closed rather than falling back to a Run ID or clock.

Issuer repository defaults strictly to the target repository from ControlPolicy (official CANN target in the active design). A different emitter repository requires explicit allow_external_issuer=true in reviewed deployment policy in addition to exact issuer path/ID/workflow-source/key pins. This is a separate security decision needing user approval; an allowed personal source fork is NOT automatically an allowed emitter. The candidate remains disabled.
