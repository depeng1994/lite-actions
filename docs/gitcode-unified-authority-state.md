# Unified authority and Coordinator state — candidate v5

This candidate uses one SQLite database for GitCode authenticated observations, immutable source intents/attestations, execution generations, cancellation outboxes and physical lease records. It does not create another scheduler or listener.

Blue and operator Bridge assembly both bind `<state_directory>/coordinator.sqlite3`. Source tables are namespaced `source_observations`, `source_intents`, `source_invalidations`, `source_attestations`; repository ID and PR number scope the observation/history tables. Existing Coordinator intent/execution tables retain their names and meaning.

Within one `BEGIN IMMEDIATE`, the provider reads authenticated current API state, persists source observation/invalidation, applies Coordinator version generation/revocation and cancellation outbox changes, and registers a new source intent/attestation. A crash or SQL failure after observation but before intent registration rolls back all of those mutations. API-read failure before mutation persists `available=0`; it never grants new execution or current PASS. Broker refresh calls the provider shared transaction directly, avoiding nested SQLite connections/deadlock.

Signed ingress requires an explicitly shared Coordinator. A standalone unit-test provider also uses the Coordinator schema but is not proof that an external Hosted runner shares state. Hosted current authority still uses the separately documented artifact/fixed-Check challenge protocol, never a fresh local history ledger.

Migration from Coordinator state v1–v4 retains requests, intents, executions, cases, leases, result acknowledgments and retry/cancellation outboxes. Current recorded PR versions are copied into the new source observation table; because older Coordinator versions did not retain issuer invalidation times, migration establishes an explicit current-time enrollment barrier rather than inventing prior history. Unknown old signed events cannot be retrospectively enrolled before that barrier.

An old separate `authority.sqlite3` is NOT silently ignored or merged. Assembly refuses it and requires an independently reviewed migration/retirement plan. Do not erase that ledger to make startup pass. Old Coordinator binaries cannot read v5; do not lower `user_version` or overwrite current leases/outboxes from a historical backup. Rollback requires a compatible binary or reviewed atomic conversion preserving every newer revocation and lease.

Tests: seven focused CPU tests prove immediate old-grant revocation for observed ABA, complete rollback via SQL failure injection, immediate prior-spec cancellation, durable authority-unavailable refusal, two repositories sharing PR number 1 without collision, v4 upgrade preserving pending executions/leases/outboxes plus enrollment barrier, and signed-mode refusal without explicit shared Coordinator. No real credentials, production database, service deployment or hardware was used.
