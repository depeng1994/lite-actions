# Explicit GitCode cross-fork source contract

User scope correction: actual TorchTitan PR targets the official GitCode repository from the personal fork, not an internal personal PR.

Trusted release configuration independently pins target PR/base repository ID `9028822` and allowed personal head `depeng1994/torchtitan-npu`, ID `9075802`. Target path must equal the independently verified API full_name, not a path/clone URL copied from PR payload. Allowed target refs/authors and U=C/default-branch release gates remain unchanged.

Policy addition:
```json
"allowed_head_repositories": [["depeng1994/torchtitan-npu", "9075802"]]
```

The legacy assembly key `personal_repo` identifies the **PR authority/target repository**. Here it must be the verified official path, with policy.gitcode_repo_id `9028822`, NOT the head-fork path. The new allowlist identifies source separately; naming is retained for compatibility.

The client independently resolves configured source/target IDs even when PR embedded IDs are missing. H comes from configured source branch; current B comes from target branch, never cached PR base.sha. Unknown paths, reassigned IDs, embedded-ID conflicts and target mismatch fail closed before following any supplied URL.

Prepare fetches both roles into an isolated merge object database using only the two configured HTTPS URLs, verifies exact SHAs, requires real common ancestor, computes M/T, and brackets with fresh observations plus both repository identity checks. No GitHub fallback or PR clone URL is added.

Generic validate_pr remains same-repository by default. Policy callers pass configured head IDs. Ledger/source-manifest structural checks explicitly use allowed_head_repo_ids=None only on already authenticated objects; this is NOT admission authorization. Target/base IDs must always match.

Read-only CLI can explicitly pass `--head-repository depeng1994/torchtitan-npu=9075802` to `python -m ci_core.gitcode_api`; credentials remain environment-only.

Evidence: seven new CPU tests cover missing IDs, source reassignment, arbitrary fork rejection, target mismatch, retarget revocation, strict/default contract gating and a REAL LOCAL-GIT merge from two repositories preserving both advanced target content and personal Head content. Test-only local transport does not weaken production HTTPS-only fetch. Broader API/Prepare/contract/Bridge/Artifact/assembly/state/platform regression: 115 tests passed.

No live PR mutation, hardware execution, deployment, credential setup or publication was performed.
