# Personal GitCode manual-forward workflow adapter

This package contains the reviewed manual capture, forward/receipt validation, per-case result display, and independent aggregate gate logic used by the personal TorchTitan GitCode workflow.

The model repository is intentionally a thin workflow configuration. Its only inline program downloads a public Lite Actions archive at one exact commit, verifies the full archive SHA-256, extracts only `ci_core/` with path/type/size checks, and invokes `ci_core.gitcode_manual.cli`. No new service, credential, authorization protocol, or mutable `latest` dependency is introduced.

Phases are a fixed allowlist: `capture`, `forward`, `result`, and `aggregate`. The capture step's platform command digest is bound into the release policy and rechecked by the forward adapter. Only the forward job receives the existing GitHub dispatch/read credential; result and aggregate jobs receive no GitCode or GitHub credential.

The result adapter retains canonical request, receipt, slot, identity, generation, nonce, cleanup, and current-authority checks. The aggregate gate is historical presentation only and explicitly reports `current_authority=NOT_PROVIDED` and `merge_gate=NOT_PROVIDED`.
