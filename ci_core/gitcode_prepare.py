"""Prepare immutable GitCode sources without executing the proposed PR.

The source probe is deliberately read-only and does not create a Plan, result
slot, admission receipt, device lease or merge permission. Production callers
must additionally satisfy the schema-4 release and admission contracts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ci_core.gitcode_api import GitCodeClient
from ci_core.gitcode_contract import (
    MODEL_REPOSITORIES,
    ControlPolicy,
    canonical,
    shape,
    validate_pr,
)
from ci_core.gitcode_source import MergeSourceBuilder, SourceError, SourceHandle


class PreparationError(ValueError):
    pass


def verify_control_release(
    policy: ControlPolicy,
    *,
    upstream: dict,
    repository: dict,
    branch: dict,
    workflow: dict,
) -> None:
    """Require actual published GitCode U == GitHub default-branch C.

    Arguments are independent API observations, not workflow inputs. A branch
    named 'ci' and a same-name repository with another numeric ID cannot satisfy
    this check. Publishing code to the upstream remains a separate human action.
    """
    repo, default_ref = MODEL_REPOSITORIES[policy.github_repo_id]
    if (
        upstream.get("repo_id") != policy.upstream_repo_id
        or upstream.get("default_ref") != policy.upstream_default_ref
        or upstream.get("sha") != policy.control_sha
        or repository.get("id") != policy.github_repo_id
        or repository.get("full_name") != repo
        or repository.get("default_branch") != default_ref
        or branch.get("name") != default_ref
        or (branch.get("commit") or {}).get("sha") != policy.control_sha
        or workflow.get("id") != policy.workflow_id
        or workflow.get("path") != policy.workflow_path
        or workflow.get("state") != "active"
    ):
        raise PreparationError("CONTROL_RELEASE_NOT_PUBLISHED_ON_DEFAULT_BRANCH")


def prepare_pr_source(
    client: GitCodeClient,
    number: int,
    destination: Path,
    *,
    allowed_base_refs: tuple[str, ...],
    allowed_actors: tuple[str, ...],
    expected_git_version: str,
    expected_identity: dict | None = None,
    optional_gitlinks: dict[str, str] | None = None,
    builder_factory=MergeSourceBuilder,
) -> tuple[SourceHandle, dict]:
    """Bracket fetch/build with authoritative reads and publish no stale bundle.

    The builder fetches refs with full history and checks the fetched object IDs.
    No PR-supplied module, merge driver, hook, submodule URL or credential is used.
    Dependency exceptions must be reviewed deployment data, not caller payload.
    """
    before = client.open_pull_snapshot(
        number,
        allowed_base_refs=allowed_base_refs,
        allowed_actors=allowed_actors,
    )
    shape(before, {"identity", "state", "authorized"})
    identity = validate_pr(
        before["identity"],
        allowed_repo_id=client.repo_id,
        allowed_refs=allowed_base_refs,
        allowed_head_repo_ids=tuple(client.allowed_head_repositories.values())
        if isinstance(getattr(client, "allowed_head_repositories", None), dict)
        else (),
    )
    if before["state"] != "open" or before["authorized"] is not True:
        raise PreparationError("PR_SOURCE_NOT_AUTHORIZED")
    if expected_identity is not None and canonical(identity) != canonical(
        expected_identity
    ):
        raise PreparationError("DISPATCH_SNAPSHOT_NO_LONGER_CURRENT")
    metadata = client.repository()
    source_metadata = (
        metadata
        if identity["head_repo_id"] == identity["repo_id"]
        else client.repository_for_identity(identity, "head")
    )
    if source_metadata["repo_id"] != identity["head_repo_id"]:
        raise PreparationError("SOURCE_REPOSITORY_IDENTITY_MISMATCH")
    builder = builder_factory(
        Path(destination),
        expected_git_version=expected_git_version,
        allowed_urls=tuple(
            dict.fromkeys((metadata["clone_url"], source_metadata["clone_url"]))
        ),
    )
    try:
        for role in ("base", "head"):
            builder.fetch_ref(
                url=(metadata if role == "base" else source_metadata)["clone_url"],
                ref=identity[role + "_ref"],
                expected_sha=identity[role + "_sha"],
                role=role,
            )
        handle = builder.build(
            head_sha=identity["head_sha"],
            base_sha=identity["base_sha"],
            optional_gitlinks=optional_gitlinks,
        )
        after = client.open_pull_snapshot(
            number,
            allowed_base_refs=allowed_base_refs,
            allowed_actors=allowed_actors,
        )
        source_after = (
            client.repository()
            if identity["head_repo_id"] == identity["repo_id"]
            else client.repository_for_identity(identity, "head")
        )
        if (
            canonical(before) != canonical(after)
            or client.repository() != metadata
            or source_after != source_metadata
        ):
            raise PreparationError("PR_SOURCE_CHANGED_DURING_PREPARATION")
        handle.verify()
        return handle, identity
    except BaseException:
        # Never leave a complete-looking archive after an authority/network error.
        (builder.root / "source.tar.gz").unlink(missing_ok=True)
        raise


def manifest_for_source(
    handle: SourceHandle,
    *,
    pr: dict,
    merge: dict,
    control: dict,
    github_run_id: int,
    run_attempt: int,
    source_artifact_id: int,
) -> dict:
    """Create Manifest 2 after uploading the source, before sealing the Plan.

    The source-manifest has no Plan digest: a Plan refers to this artifact, not
    the reverse. Artifact ZIP digest verification remains the receiver's job.
    """
    from ci_core.gitcode_contract import MERGE_KEYS, positive

    handle.verify()
    validate_pr(pr, allowed_head_repo_ids=None)
    shape(merge, MERGE_KEYS)
    for value in (github_run_id, run_attempt, source_artifact_id):
        positive(value)
    if run_attempt != 1:
        raise PreparationError("NATIVE_WORKFLOW_RERUN_NOT_SUPPORTED")
    if (
        pr["head_sha"] != handle.head_sha
        or pr["base_sha"] != handle.base_sha
        or merge["tested_merge_sha"] != handle.tested_merge_sha
        or merge["tested_tree_sha"] != handle.tested_tree_sha
        or merge["policy"] != handle.merge_policy
        or merge["builder_version"] != "git-" + handle.git_version
    ):
        raise SourceError("SOURCE_HANDLE_MANIFEST_IDENTITY_MISMATCH")
    result = {
        "schema": 2,
        "source_provider": "gitcode",
        "test_mode": "merge_result",
        "pr": pr,
        "merge": merge,
        "control": control,
        "github_run_id": github_run_id,
        "run_attempt": run_attempt,
        "source_artifact_id": source_artifact_id,
        "bundle_sha256": handle.bundle_sha256,
        "dependencies": list(handle.dependencies),
    }
    canonical(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--base-ref", action="append", required=True)
    parser.add_argument("--actor", action="append", required=True)
    parser.add_argument("--git-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    handle, identity = prepare_pr_source(
        GitCodeClient.from_environment(args.repo, args.repo_id),
        args.pr,
        args.output,
        allowed_base_refs=tuple(args.base_ref),
        allowed_actors=tuple(args.actor),
        expected_git_version=args.git_version,
    )
    observation = {
        "validation_scope": "SOURCE_PREPARATION_ONLY",
        "identity": identity,
        "source": handle.metadata(),
        "hardware_execution": "NOT_RUN",
        "event_revision_proof": "NOT_ESTABLISHED",
        "merge_gate": "NOT_PROVIDED",
    }
    (args.output / "source-observation.json").write_bytes(
        canonical(observation) + b"\n"
    )
    print(json.dumps(observation, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
