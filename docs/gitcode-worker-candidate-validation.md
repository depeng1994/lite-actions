# Worker candidate validation — 2026-11-02

Base: 9cfb056a7d4125966adf6b454172c0396e4039ec. Local isolated candidate, not deployed or remotely published by this implementation task.

## Verified code evidence

- Full unittest discovery: 424 tests PASS (36.586 s); ruff check, ruff format --check, compileall and git diff --check PASS.
- Full log: /root/gitcode-forward-work/tt-live-adapters-final-regression.log.
- Independent signed-ingress review/rerun: 9 tests PASS; log /root/gitcode-forward-work/tt-signed-ingress-independent-final.log.
- Independent review closed delayed old-spec supersession and terminal Action revival defects. Duplicate capture cancellation is isolated from the original canonical owner; verified rejected first-enrollment terminal captures retain non-executable tombstones across restart.
- Real SDK Producer/Reader/Broker/Coordinator exercised with synthetic local signatures: early signed registration creates no execution; only completed validated Prepare permits admission. No hardware lease is minted by this test.
- Model agent separately exercised final model c6f97f7 runtime through actual assemble_hosted and Producer and synthetic RSA sidecar transport. Outbound APIs deliberately fail closed; this is not positive live platform E2E.

## State and rollback

State version 5 unifies source observations, immutable intents, invalidations and attestations with Coordinator generations/leases/outbox. v4 migration retains existing state and installs a conservative original-event enrollment barrier. Separate legacy authority.sqlite3 is rejected, not deleted/imported implicitly. Old binaries cannot read v5: rollback requires a compatible reviewed binary or reviewed state conversion. Never lower user_version, erase the ledger, or restore an older database that loses leases, revocations or outbox history.

## Remaining acceptance gates

Real protected GitCode hosted original-event identity/time and secret restrictions, separate emitter signing authorization/public-key pin, reviewed exact SDK/control/default-branch deployment config, native Actions run and current result evidence, single existing Blue controlled validation, review/CLA/upstream merge remain external gates. Parent has separately verified the dedicated Checks App/key installation; no real secret was read by these tests. Historical PR comments are evidence only, not native GitCode Check success or merge approval. Automatic Actions enrollment remains disabled without reviewed provenance; manual/periodic tests do not satisfy it.

## Reader observation boundary correction

Independent review identified that stale early-intent rejection could occur after the Blue reader directly observed B but before the shared ledger saw it. All Blue/Bridge reader snapshot calls now route through the durable authority transaction and return that exact committed snapshot; Hosted retains no fresh authority ledger. Regression drives an existing canonical A through reader-observed B rejection before broker refresh, then restores A and verifies revision advancement and permanent old execution/plan revocation. No exception-time second read or untrusted intent snapshot can fabricate the B observation.

Final corrected regression: 425 tests PASS (34.575 s), ruff check/format, compileall, diff check PASS. Log: /root/gitcode-forward-work/tt-worker-observation-final-regression.log. Independent audit reran all 24 signed-ingress/assembly tests PASS and confirmed this observation boundary closed.
