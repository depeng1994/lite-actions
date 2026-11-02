# Personal staging profile — code candidate, not activated

This profile does not change production defaults or claim official CANN acceptance.

## Exact bindings

- `deployment_profile`: `personal-staging` (explicit; default remains `production`).
- GitHub control: `depeng1994/torchtitan-npu`, numeric ID `1245430804`, default `master`.
- GitCode control U: `depeng1994/torchtitan-npu`, ID `9075802`, default `master`.
- GitCode PR target, base and head repository: `9075802`; this profile cannot be used to label an official CANN PR as personal staging input.
- `personal_repo` and `upstream_repo`: both `depeng1994/torchtitan-npu`.
- `control_sha`: exact reviewed commit present at both default branches, staging U=C. Existing independent API verification is unchanged; no test branch, wrong repository ID, or unequal commit is accepted.
- `app_id` / `checks_installation_id`: separately approved personal staging App/installation, not production App 5575236. No values are fabricated here.
- Existing exact workflow paths, API-observed workflow ID, SDK commit, runner digest, catalog and actors still require reviewed configuration.

The existing official GitHub profile remains `1210038933 / cann-framework/torchtitan-npu / master`. The schema-3 repository allowlist is unchanged. Personal staging is schema-4 only; no environment setting implicitly changes global production identity. Source interface validation applies to both TorchTitan profiles.

## Single Blue and isolation

One existing Blue process can poll its existing schema-3 repositories plus the explicit schema-4 profile's bound client. No second service, new resource pool, lock namespace, or physical device identity is introduced. All execution continues through the existing Router and physical resource/host-budget/port locks. OS-only probes do not acquire hardware resources.

For this minimal extension, an assembly cannot mix official and personal schema-4 profiles. Use a separately approved persistent staging state directory with its own binding file and Coordinator, and never rebind an existing directory to another GitHub profile. Existing state files are not erased or silently migrated by this patch. A legacy binding missing the new repository/profile fields fails closed and requires explicit reviewed migration; do not delete it to bypass the check.

Runtime broker, publisher and authority-responder keys include GitHub repository ID, control SHA and workflow ID. Request IDs include repository ID; incoming Run path/ID, artifacts, Checks App, retained plan and cancel target must match that exact configured client. Restoring a request cannot alias another repository's same numeric Run ID. Unknown source/API state remains fail-closed.

## Activation gates, not performed here

Publishing either master, registering/installing a staging App, generating/storing its private key, issuing/configuring credentials and changing Blue deployment require the parent's separate authorization. Do not reuse the official App private key. Protected GitCode Actions event provenance and any independent emitter signing authorization remain mandatory; this extension does not fabricate an event or enable automatic PR entry. A personal Push/Manual result is not official PR-trigger acceptance.

Formal official CANN control publication, official U=C validation and final acceptance remain separate after staging succeeds.

## Reviewed candidate evidence

Local full regression: 439 tests PASS (33.587 s); independently rerun 439 tests PASS (34.948 s), including 14 focused staging/runtime isolation tests. Exact pinned Ruff 0.17.3 `scripts/lint.py`, compileall and diff checks passed. Logs: `/root/gitcode-forward-work/tt-worker-staging-final.log` and `/tmp/tt-staging-independent-review.log`. These are CPU/synthetic platform tests, not staging live acceptance.

Switching the one Blue between schema-4 profiles requires draining/reconciling outstanding requests, leases, cancellations and publication history plus a reviewed state/config transition. Never silently discard the previous ledger or delete its binding file. No process restart, migration, credential setup, or deployment was performed as part of this candidate.
