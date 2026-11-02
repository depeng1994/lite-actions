# Optional schema-4 fresh authority channel

Status: isolated implementation with CPU fixtures; not live GitCode/GitHub/NPU acceptance.

The explicit Plan capability `authority_protocol: artifact-check-v1` adds a precreated fixed `result_slots.authority` Check Run. The capability is included in the semantic digest. Existing schema-3 and schema-4 Plans without the capability retain their existing contracts; a Hosted current-authority client does not silently downgrade to H/B comparison.

## Pull-only transport

1. Hosted waits for complete case results and an aggregate result before requesting current authority.
2. It persists a random nonce and an exact owner/target binding before upload. One immutable artifact per owning Run/Attempt is named `lite-authority-<run_id>-1`, with sole member `authority-challenge.json`.
3. The challenge binds both owner and canonical target request IDs, Plan digests, complete AdmissionReceipts (including all generations), and H/B/M/T/control identity. It expires within 180 seconds and both Plan deadlines.
4. Blue's existing maintenance loop reads the artifact through authenticated Run identity and exact metadata/ZIP digest checks. It resolves the target from retained VerifiedRequest state. No new listener or second service exists.
5. Before replying, Blue refreshes authenticated GitCode state, checks the shared observed-intent ledger, and checks exact persisted canonical receipt and coordinator publication generation. Failure/unknown/revocation produces no positive grant.
6. Blue writes a nonce-bound response into the owner's already allocated authority Check ID, with a maximum 15-second observation lifetime. The slot stays nonterminal while its owner is active; it is not an aggregate or merge Check. After the API independently reports the owning Run completed (including cancellation), Blue closes the exact slot with neutral, never success. A durable sidecar records confirmed closure; lost PATCH responses are reconciled at the same ID. Expired Plans remain eligible for terminal cleanup, not for fresh proof.
7. Hosted validates the exact Check/App/Run/Plan/receipt/challenge identities and freshness before accepting the aggregate as current. Missing or old proof stays pending; expired challenges fail closed.

A duplicate Waiter uploads in its own Run and reads its own fixed authority slot. Its challenge targets the canonical request; equal execution/generations/spec/source identity are mandatory. It cannot upload into or impersonate the canonical Run.

## Recovery and bounds

The client persists the challenge before upload. A lost response is recovered only through the single exact immutable artifact name and matching content. Unknown upload is never blindly retried. Restart reuses the same nonce/document. There is one challenge artifact per owning Run; expiry requires an explicitly authorized new whole-run execution, not unbounded artifact creation.

All aggregate-confirmation network I/O precedes the final Hosted freshness decision. Blue timestamps the start, not end, of its current-authority callback and refuses publication if that observation consumed the 15-second proof lifetime.

The proof is a serialized short-lived observation, not an atomic merge guarantee. It does not establish GitCode Action event authenticity, reconstruct unseen ABA history, or replace approved U=C/default-branch/Checks-App controls. `versioned_only` merge semantics remain unchanged.

## Local evidence

Thirteen focused protocol tests cover exact proof, revocation/outage, freshness/expiry, lost-upload recovery and nonretry, changed nonce/generation, duplicate owner/canonical binding, disabled protocol, complete-results-before-proof Waiter sequencing, slow aggregate/current-observation expiry, neutral-only terminal cleanup and idempotent lost cleanup responses. The earlier combined contract/Checks/Waiter/pipeline/protocol suite had 67 passing CPU tests before the four freshness/cleanup regressions were added. An independent reviewer reran all 13 final authority tests successfully and confirmed the freshness findings closed. No credentials, external artifacts, Checks, deployment or hardware were created by these tests.
