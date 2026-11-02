# GitCode manual-forward SDK ownership

The personal TorchTitan workflow's common manual capture, forward/receipt validation, per-case display, and aggregate gate logic now lives in `ci_core.gitcode_manual`. The model repository is reduced to six thin workflow job definitions and one bounded bootstrap script.

The fixed bootstrap downloads the public Lite Actions source archive by full commit SHA, verifies the archive SHA-256, extracts only the `ci_core/` subtree, rejects unsafe archive members and enforces archive/extraction byte budgets, then invokes the fixed CLI phase. It uses no GitHub/GitCode credential to fetch the public SDK archive and introduces no service or authorization protocol.

Fixed phases: `capture`, `forward`, `result`, `aggregate`. The manual capture step's exact platform script SHA is passed into the forward phase and must match the release's `ManualCapturePolicy.script_sha256`; no source log or user-controlled script is trusted. Only the forward phase has the existing GitHub credential. Case display and aggregate phases have neither GitHub nor GitCode credential.

The SDK package retains the existing checks: manual run/step double-read and capture digest; PR/head/base/control/workflow identity; one dispatch POST and nonce binding; exact GH Run/attempt, Prepare, artifact and plan; canonical receipt/slot/generation/nonce identity; case evidence and cleanup; separate per-case status; and a historical-only aggregate gate. Aggregate PASS explicitly does not provide current authority or a merge gate.

The 59 SDK manual-forward regression tests are synthetic API/CPU-only. They do not POST, dispatch, access NPU or claim hardware acceptance. The separate GitCode platform-size probes are recorded in the model-repository diagnostic evidence.
