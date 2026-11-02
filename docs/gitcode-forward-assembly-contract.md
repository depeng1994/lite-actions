# Trusted assembly contract — candidate, not deployed

The existing single Blue process gains explicit options:

    python3 unified_router.py --worker-id blue \
      --gitcode-config /trusted/releases/smoke.json \
      --gitcode-config /trusted/releases/nightly.json \
      --gitcode-state-directory /persistent/torchtitan-gitcode

Paths above are placeholders, not provisioned deployment paths. --validate validates trusted config syntax/ownership only, not live readiness. Yellow rejects these options. No config means unchanged schema-3 startup.

## Trusted JSON

Required exact keys: schema=1, sdk_sha (pinned Worker SDK commit checked by Hosted bootstrap), authority_mode=observed_history_versioned_only, policy (reviewed ControlPolicy fields), catalog (static TorchTitan registry), personal_repo, upstream_repo, github_actor, checks_installation_id. Files must be absolute, non-symlink, owner-controlled regular files with no group/world write permission. No token/private-key fields are accepted.

Optional authority_protocol=artifact-check-v1 enables the pull-only current-authority challenge: Hosted uploads an immutable nonce-bound Artifact, Blue verifies it against retained requests and exact canonical receipt/generation and answers through the fixed authority Check ID. No inbound listener or new service is introduced. Without this capability Hosted current() explicitly fails, never falls back to H/B equality. Short challenge/response expiry is enforced; expired challenge requires a new authorized execution, not replay.

Optional publication={enabled:true,actor:<approved GitCode login>} enables immutable versioned PR comment evidence through a separately provisioned credential. Default absent or {enabled:false} makes no GitCode writes. This is not a native GitCode Check, tester approval, current merge permission or proof of GitCode Action execution.

## Environment and persistent ownership

- LITE_ACTIONS_GITHUB_TOKEN: control read/dispatch/cancel credential.
- LITE_ACTIONS_GITCODE_TOKEN: GitCode authority read identity.
- LITE_ACTIONS_CHECKS_KEY_FILE: absolute owner-only RSA key file, required ONLY by Blue and Hosted prepare for the dedicated repository-scoped Checks App. Hosted wait and GitCode Bridge use a strict read-only Checks client and receive no private key.
- LITE_ACTIONS_GITCODE_PUBLICATION_TOKEN: separate credential, required only when publication is explicitly enabled.
- GITHUB_RUN_ID: current Hosted owner Run when authority_protocol is enabled.
- LITE_ACTIONS_HOSTED_PHASE: trusted prepare or wait phase, required for Hosted assembly. Wait has no Producer or Checks write capability.

No schema-4 gh auth fallback, no PAT fallback for Checks. No credential is created, printed, placed in artifacts or persisted by assembly. Existing key file is read only by the reviewed signer when minting a short-lived installation token; this patch does not authorize provisioning it.

Blue and trusted manual/periodic Bridge MUST share the same owner-only persistent directory. authority-binding.json prevents rebinding it to another source. coordinator.sqlite3 (state v5) atomically holds observed revocations, immutable intents, generations, leases and publication ownership; legacy separate authority.sqlite3 is explicitly rejected rather than silently reset; requests/ retains accepted immutable records. Per-release journals are under control_sha/workflow_id. A fresh Hosted runner directory cannot replace the authority ledger. Lost history requires recovery, not synthesized proof.

## Safety boundaries

The default Artifact reader still rejects completed Runs for admission. load_results permits completed Runs only for complete Prepare/artifact/manifest/slot validation and returns immutable VerifiedRequest metadata without retained executable source. A successful Workflow conclusion is not a successful test.

Every final admission, startable-source check, pre-launch guard and publication/current-authority callback validates the trusted intent against the latest shared ledger. Exact coordinator receipt/generation ownership is required in addition to current H/B, preventing old selectors/reruns from regaining publication rights.

Blue publishes optional GitCode evidence before marking aggregate delivery acknowledged. Failed/unknown writes remain pending and cannot rerun hardware or replace fixed Check IDs. Completed and duplicate owner requests remain eligible for authority responses while their owner Run is active and their canonical execution unexpired.

The observed-history adapter supports explicitly authorized manual/periodic control-host operation only. GitCode Action event provenance is NOT fabricated from periodic IDs or environment variables. Automatic Action mode remains fail-closed until its real authenticated provenance contract is verified. No hidden/unobserved ABA or atomic merge guarantee is claimed.

## Deployment gate

No production service, credentials, permissions, Required Checks, Yellow resources or production database were modified. Real API/App evidence, verified U=C publication, reviewed state-v5 rollback compatibility and parent review remain required before deployment. Never lower user_version or overwrite newer lease/outbox state with an old backup to roll back.

## Disabled emitter attestation candidate

Optional attestation config is absent/disabled by default. See gitcode-attestation-candidate.md for exact public-key/issuer/workflow/original-event pins. Blue receives only the public key; no dedicated signing key is provisioned here. Signed metadata remains subject to actual API identity/status and durable observed-revocation gates.

## Concrete approved Checks identity (not automatic deployment)

The parent verified App 5575236, installation 177722908, GitHub repository 1210038933, checks:write + metadata:read only. The approved Blue key path is /etc/lite-actions/credentials/torchtitan-checks.pem (root, 0600; parent directory 0700). Hosted Prepare uses the existing LITE_GITCODE_CHECKS_APP_KEY GitHub Secret, materialized to its private temporary key path. No key value belongs in this configuration or documentation. Installation tokens automatically refresh before their normal one-hour expiry; their lifetime is not extended.

Final reviewed policy must bind official GitCode target cann/torchtitan-npu / 9028822 and explicitly allow source depeng1994/torchtitan-npu / 9075802, plus exact GitHub repository, C/U commit, default branch, workflow path and numeric workflow ID, registry/selectors and App ID. Actual release SHA and workflow metadata remain deployment inputs, never guessed. Public attestation key fingerprint and protected capture workflow/run original-event contract still require separate review/authorization.

Signed Action Prepare does not need Blue generation in its immutable Plan. The model uploads its signed sidecar before Producer; Blue verifies/enrolls it atomically but will not admit executable sources until real Prepare completion and full artifact validation. No separate early ingress Check or shared filesystem on a GitCode hosted runner is assumed.
